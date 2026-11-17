import re

# Standard JK Tyre Zones (fallback or validation set)
STANDARD_JK_ZONES = {"NZ", "WZ", "CZ", "EZ", "SZ", "TZ", "NP"}

HIERARCHY_RANKS_SQL = """
CREATE TABLE IF NOT EXISTS `hierarchy_ranks` (
    `id` INT PRIMARY KEY,
    `rank_name` VARCHAR(50) NOT NULL,
    `description` VARCHAR(150) NOT NULL,
    `scope_level` ENUM('NATIONAL', 'ZONE', 'REGION', 'TERRITORY') NOT NULL,
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

USER_HIERARCHY_ACCESS_SQL = """
CREATE TABLE IF NOT EXISTS `user_hierarchy_access` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `user_id` INT UNSIGNED NOT NULL,
    `rank_id` INT NOT NULL DEFAULT 4,
    `designation` VARCHAR(150) NULL,
    `department` VARCHAR(100) DEFAULT 'Sales',
    `zone_code` VARCHAR(50) NOT NULL DEFAULT 'ALL',
    `region_code` VARCHAR(50) NOT NULL DEFAULT 'ALL',
    `territory_code` VARCHAR(50) NOT NULL DEFAULT 'ALL',
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    `updated_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY `uk_user_hierarchy_user` (`user_id`),
    INDEX `idx_user_hierarchy_scope` (`zone_code`, `region_code`, `territory_code`),
    INDEX `idx_user_hierarchy_rank` (`rank_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

DEFAULT_RANKS_SEED = [
    (1, 'National Head', 'VP / Director / Pan-India Scope', 'NATIONAL'),
    (2, 'Zonal Head', 'Zone Level Leadership', 'ZONE'),
    (3, 'Regional Manager', 'Region Level Management', 'REGION'),
    (4, 'Field Executive', 'Territory / Field Sales Incharge', 'TERRITORY'),
]


def ensure_hierarchy_tables(conn):
    """
    Ensures `hierarchy_ranks` and `user_hierarchy_access` tables exist.
    Leaves the core `users` table completely untouched.
    """
    if not conn:
        return
    cursor = None
    try:
        cursor = conn.cursor()
        # 1. Create hierarchy_ranks master table
        cursor.execute(HIERARCHY_RANKS_SQL)
        
        # 2. Seed default ranks if not present
        seed_sql = """
            INSERT INTO `hierarchy_ranks` (`id`, `rank_name`, `description`, `scope_level`)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE `rank_name`=VALUES(`rank_name`), `description`=VALUES(`description`);
        """
        for r_row in DEFAULT_RANKS_SEED:
            cursor.execute(seed_sql, r_row)

        # 3. Create user_hierarchy_access mapping table
        cursor.execute(USER_HIERARCHY_ACCESS_SQL)
        conn.commit()
        print("[Auto-Setup] Hierarchy master & access mapping tables verified successfully.", flush=True)

    except Exception as e:
        print(f"[Auto-Setup Warning] Hierarchy table check note: {e}", flush=True)
    finally:
        if cursor:
            try: cursor.close()
            except Exception: pass


def load_master_hierarchy_cache(conn):
    """
    Loads zones and zone-to-regions mapping from database or master views.
    Returns: (valid_zones_set, zone_to_regions_dict, valid_territories_set)
    """
    valid_zones = set(STANDARD_JK_ZONES)
    zone_to_regions = {}
    valid_territories = set()

    cursor = None
    try:
        if conn:
            cursor = conn.cursor(dictionary=True) if hasattr(conn, "cursor") else conn.cursor()
            
            tables_to_check = [
                "sales_revenue_plan_vs_sale",
                "dashboard_sales_by_zone",
                "sales_summary_domestic_sales_value_achievement"
            ]
            
            for tbl in tables_to_check:
                try:
                    cursor.execute(f"SHOW TABLES LIKE '{tbl}'")
                    if cursor.fetchone():
                        cursor.execute(f"SELECT DISTINCT zone, region FROM {tbl} WHERE zone IS NOT NULL")
                        for r in cursor.fetchall():
                            z = str(r.get("zone", "")).strip().upper() if isinstance(r, dict) else str(r[0]).strip().upper()
                            reg = str(r.get("region", "")).strip().upper() if isinstance(r, dict) else str(r[1]).strip().upper()
                            if z:
                                valid_zones.add(z)
                                if z not in zone_to_regions:
                                    zone_to_regions[z] = set()
                                if reg:
                                    zone_to_regions[z].add(reg)
                        break
                except Exception:
                    continue
    except Exception as e:
        print(f"[Master Cache] Warning loading hierarchy: {e}", flush=True)
    finally:
        if cursor:
            try: cursor.close()
            except Exception: pass

    return valid_zones, zone_to_regions, valid_territories


def validate_import_dataframe(conn, df):
    """
    Validates the entire uploaded user dataframe.
    Atomic rule: If even 1 row fails, entire import is rejected.
    Returns: (is_valid: bool, errors: list, cleaned_users: list)
    """
    errors = []
    cleaned_users = []

    if df is None or len(df) == 0:
        return False, [{"row": 0, "field": "File", "value": "", "reason": "Uploaded file is completely empty."}], []

    # 1. Standardize column names
    df.columns = [str(col).strip().lower().replace(" ", "_") for col in df.columns]

    # Required column mapping
    required_cols = ["full_name", "email", "rank", "designation", "zone_code", "region_code"]
    aliases = {
        "name": "full_name",
        "user_name": "full_name",
        "user_email": "email",
        "rank_level": "rank",
        "rank_id": "rank",
        "role": "designation",
        "zone": "zone_code",
        "region": "region_code",
        "territory": "territory_code"
    }
    for orig, alias in aliases.items():
        if orig in df.columns and alias not in df.columns:
            df.rename(columns={orig: alias}, inplace=True)

    missing_cols = [c for c in required_cols if c not in df.columns]
    if missing_cols:
        return False, [{
            "row": 1,
            "field": "Headers",
            "value": ", ".join(missing_cols),
            "reason": f"Missing mandatory columns: {', '.join(missing_cols)}. Please use the standard template."
        }], []

    # 2. Pre-fetch existing emails from DB
    existing_emails = set()
    cursor = None
    try:
        cursor = conn.cursor(dictionary=True) if hasattr(conn, "cursor") else conn.cursor()
        cursor.execute("SELECT LOWER(TRIM(email)) as email FROM users WHERE email IS NOT NULL")
        for r in cursor.fetchall():
            em = r.get("email") if isinstance(r, dict) else r[0]
            if em:
                existing_emails.add(str(em).lower().strip())
    except Exception as e:
        print(f"[Validator] Warning fetching emails: {e}", flush=True)
    finally:
        if cursor:
            try: cursor.close()
            except Exception: pass

    # 3. Load Master Hierarchy Cache
    valid_zones, zone_to_regions, valid_territories = load_master_hierarchy_cache(conn)

    seen_emails_in_file = {}
    email_regex = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

    # 4. Row-by-Row Validation (Valid rows will be processed, invalid rows skipped)
    for idx, row in df.iterrows():
        row_num = idx + 2  # 1-based index including header
        row_errors_before = len(errors)
        
        # A. Full Name
        full_name = str(row.get("full_name", "")).strip()
        if not full_name or full_name.lower() in ("nan", "none", "null"):
            errors.append({
                "row": row_num,
                "field": "Full Name",
                "value": full_name,
                "reason": "Full Name is mandatory and cannot be empty."
            })

        # B. Email
        email = str(row.get("email", "")).strip().lower()
        if not email or email in ("nan", "none", "null"):
            errors.append({
                "row": row_num,
                "field": "Email",
                "value": "",
                "reason": "Email is mandatory and cannot be empty."
            })
        elif not email_regex.match(email):
            errors.append({
                "row": row_num,
                "field": "Email",
                "value": email,
                "reason": "Invalid email format."
            })
        elif email in seen_emails_in_file:
            errors.append({
                "row": row_num,
                "field": "Email",
                "value": email,
                "reason": f"Duplicate email in file (already present on row {seen_emails_in_file[email]})."
            })
        elif email in existing_emails:
            errors.append({
                "row": row_num,
                "field": "Email",
                "value": email,
                "reason": "A user with this email already exists in the system database."
            })
        else:
            seen_emails_in_file[email] = row_num

        # C. Rank
        rank_raw = str(row.get("rank", "")).strip()
        rank_val = None
        if not rank_raw or rank_raw.lower() in ("nan", "none", "null"):
            errors.append({
                "row": row_num,
                "field": "Rank",
                "value": rank_raw,
                "reason": "Rank is mandatory (1=National, 2=Zonal Head, 3=Regional Manager, 4=Field Executive)."
            })
        else:
            try:
                rank_val = int(float(rank_raw))
                if rank_val not in (1, 2, 3, 4):
                    errors.append({
                        "row": row_num,
                        "field": "Rank",
                        "value": rank_raw,
                        "reason": f"Rank must be 1, 2, 3, or 4. Received '{rank_raw}'."
                    })
            except ValueError:
                errors.append({
                    "row": row_num,
                    "field": "Rank",
                    "value": rank_raw,
                    "reason": f"Rank must be an integer (1-4). Received '{rank_raw}'."
                })

        # D. Designation
        designation = str(row.get("designation", "")).strip()
        if not designation or designation.lower() in ("nan", "none", "null"):
            errors.append({
                "row": row_num,
                "field": "Designation",
                "value": designation,
                "reason": "Designation is mandatory (e.g. Zonal Head, Regional Manager)."
            })

        # E. Zone Code
        zone_code = str(row.get("zone_code", "")).strip().upper()
        if not zone_code or zone_code in ("NAN", "NONE", "NULL"):
            errors.append({
                "row": row_num,
                "field": "Zone Code",
                "value": zone_code,
                "reason": "Zone Code is mandatory (use 'ALL' for National level or valid Zone Code)."
            })

        # F. Region Code
        region_code = str(row.get("region_code", "")).strip().upper()
        if not region_code or region_code in ("NAN", "NONE", "NULL"):
            errors.append({
                "row": row_num,
                "field": "Region Code",
                "value": region_code,
                "reason": "Region Code is mandatory (use 'ALL' for Zonal Head/National level or valid Region Code)."
            })

        # G. Territory Code (Optional)
        territory_code = str(row.get("territory_code", "")).strip().upper()
        if not territory_code or territory_code in ("NAN", "NONE", "NULL"):
            territory_code = "ALL"

        # H. Department (Optional)
        department = str(row.get("department", "")).strip()
        if department.lower() in ("nan", "none", "null", ""):
            department = "Sales"

        # I. Rank vs Hierarchy Consistency Rules
        if rank_val is not None:
            if rank_val == 1:
                # Rank 1: National level -> Zone and Region must be ALL
                if zone_code != "ALL":
                    errors.append({
                        "row": row_num,
                        "field": "Zone Code",
                        "value": zone_code,
                        "reason": "Rank 1 (National/VP/Director) must have Zone Code set to 'ALL'."
                    })
                if region_code != "ALL":
                    errors.append({
                        "row": row_num,
                        "field": "Region Code",
                        "value": region_code,
                        "reason": "Rank 1 (National/VP/Director) must have Region Code set to 'ALL'."
                    })
                territory_code = "ALL"

            elif rank_val == 2:
                # Rank 2: Zonal Head -> Specific Zone, Region=ALL
                if zone_code == "ALL":
                    errors.append({
                        "row": row_num,
                        "field": "Zone Code",
                        "value": zone_code,
                        "reason": "Rank 2 (Zonal Head) cannot have Zone Code 'ALL'. Please provide specific Zone."
                    })
                elif zone_code not in valid_zones:
                    errors.append({
                        "row": row_num,
                        "field": "Zone Code",
                        "value": zone_code,
                        "reason": f"Zone Code '{zone_code}' is not a valid SAP Zone (Allowed: {', '.join(sorted(valid_zones))})."
                    })
                region_code = "ALL"
                territory_code = "ALL"

            elif rank_val == 3:
                # Rank 3: Regional Manager -> Specific Zone, Specific Region
                if zone_code == "ALL":
                    errors.append({
                        "row": row_num,
                        "field": "Zone Code",
                        "value": zone_code,
                        "reason": "Rank 3 (Regional Manager) must have a specific Zone Code."
                    })
                elif zone_code not in valid_zones:
                    errors.append({
                        "row": row_num,
                        "field": "Zone Code",
                        "value": zone_code,
                        "reason": f"Zone Code '{zone_code}' is not valid."
                    })

                if region_code == "ALL":
                    errors.append({
                        "row": row_num,
                        "field": "Region Code",
                        "value": region_code,
                        "reason": "Rank 3 (Regional Manager) must specify a Region Code (cannot be 'ALL')."
                    })
                elif zone_code in zone_to_regions and zone_to_regions[zone_code]:
                    if region_code not in zone_to_regions[zone_code]:
                        errors.append({
                            "row": row_num,
                            "field": "Region Code",
                            "value": region_code,
                            "reason": f"Region Code '{region_code}' does not belong to Zone '{zone_code}'."
                        })
                territory_code = "ALL"

            elif rank_val == 4:
                # Rank 4: Field / Territory Executive
                if zone_code == "ALL" or region_code == "ALL":
                    errors.append({
                        "row": row_num,
                        "field": "Zone/Region",
                        "value": f"{zone_code}/{region_code}",
                        "reason": "Rank 4 (Field Executive) must have both specific Zone and Region codes."
                    })

        # Only add to cleaned candidates if this specific row passed all validations
        if len(errors) == row_errors_before:
            seen_emails_in_file[email] = row_num
            cleaned_users.append({
                "name": full_name,
                "email": email,
                "department": department,
                "rank_id": rank_val or 4,
                "designation": designation,
                "zone_code": zone_code,
                "region_code": region_code,
                "territory_code": territory_code
            })

    return bool(cleaned_users), errors, cleaned_users
