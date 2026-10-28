"""
database/kgraph_backup_service.py
==================================
K-Graph ArangoDB Backup Service

Call this before _sync_to_arango() to take a safe backup of the current graph.
If data exists, backup is MANDATORY — failure raises RuntimeError and aborts sync.
If graph is empty (first run), backup is skipped and sync proceeds normally.

Usage:
    from database.kgraph_backup_service import backup_kgraph_collections
    backup_kgraph_collections(db, nodes_col_name, edges_col_name)
"""

from datetime import datetime

_BACKUP_SUFFIX = "_backup_"


def backup_kgraph_collections(db, nodes_col_name: str, edges_col_name: str, max_backups: int = 5) -> dict:
    """
    Takes a safe in-database backup of the current K-Graph before new data is synced.

    Steps:
      1. Checks current node and edge count via AQL
      2. If data exists: deletes all previous backup collections
      3. Creates new timestamped backup collections, e.g.:
             session_nodes_backup_20260911_105800_nodes_45_edges_120
             session_edges_backup_20260911_105800_nodes_45_edges_120
      4. Copies data atomically via AQL (no Python memory roundtrip)
      5. On backup failure: rolls back partial collections and raises RuntimeError
         — the caller's sync is fully aborted

    Args:
        db             : python-arango database handle
        nodes_col_name : vertex collection name (e.g. "session_nodes")
        edges_col_name : edge collection name   (e.g. "session_edges")

    Returns:
        {
          "skipped"        : bool,   # True = graph was empty, backup not needed
          "vertex_backup"  : str,    # name of created vertex backup collection
          "edge_backup"    : str,    # name of created edge backup collection
          "nodes_backed_up": int,
          "edges_backed_up": int,
          "deleted_backups": list,   # old backup collection names that were removed
        }

    Raises:
        RuntimeError -- if backup fails and existing data would be overwritten
    """

    result = {
        "skipped":         False,
        "vertex_backup":   None,
        "edge_backup":     None,
        "nodes_backed_up": 0,
        "edges_backed_up": 0,
        "deleted_backups": [],
    }

    # Step 1: Inspect current graph state
    print(f"[KGraph-Backup] Entering backup for '{nodes_col_name}' / '{edges_col_name}'", flush=True)
    try:
        cur_nodes = next(db.aql.execute(f"RETURN LENGTH({nodes_col_name})"), 0) or 0
        cur_edges = next(db.aql.execute(f"RETURN LENGTH({edges_col_name})"), 0) or 0
    except Exception as count_err:
        print(f"[KGraph-Backup] WARNING -- could not read collection counts: {count_err}. Assuming non-empty.", flush=True)
        cur_nodes, cur_edges = 1, 0   # treat as non-empty so backup proceeds
    print(f"[KGraph-Backup] Current state -- nodes: {cur_nodes}, edges: {cur_edges}", flush=True)

    if cur_nodes == 0 and cur_edges == 0:
        print("[KGraph-Backup] K-Graph is empty (first run or purged) -- backup skipped.", flush=True)
        result["skipped"] = True
        return result
    print("[KGraph-Backup] Graph is non-empty -- proceeding with mandatory backup...", flush=True)

    # Step 2: Enforce retention policy (keep `max_backups - 1` newest old backups, plus the 1 we are creating)
    # e.g. session_nodes_backup_20260911_... / session_edges_backup_20260911_...
    known_prefixes = (nodes_col_name, edges_col_name)
    all_colls   = db.collections()
    old_backups = [
        c["name"] for c in all_colls
        if _BACKUP_SUFFIX in c["name"]
        and not c["name"].startswith("_")
        and any(c["name"].startswith(p) for p in known_prefixes)
    ]
    
    # We group by the timestamp part of the name to keep node/edge collection pairs together.
    # Name format: {prefix}_backup_{timestamp}_nodes_{n}_edges_{e}
    groups = {}
    for coll in old_backups:
        parts = coll.split(_BACKUP_SUFFIX)
        if len(parts) > 1:
            # Extract timestamp which is between _backup_ and _nodes_
            ts_part = parts[1].split("_nodes_")[0]
            groups.setdefault(ts_part, []).append(coll)
            
    # Sort timestamps descending (newest first)
    sorted_ts = sorted(groups.keys(), reverse=True)
    
    # Retain the newest (max_backups - 1)
    retention_limit = max(0, max_backups - 1)
    ts_to_delete = sorted_ts[retention_limit:]
    
    collections_to_delete = []
    for ts in ts_to_delete:
        collections_to_delete.extend(groups[ts])

    print(f"[KGraph-Backup] Found {len(sorted_ts)} existing backup sets. Purging {len(ts_to_delete)} oldest sets.", flush=True)
    for old in collections_to_delete:
        try:
            db.delete_collection(old)
            print(f"[KGraph-Backup] Deleted old backup: {old}", flush=True)
            result["deleted_backups"].append(old)
        except Exception as de:
            print(f"[KGraph-Backup] WARNING -- could not delete '{old}': {de}", flush=True)

    # Step 3: Build timestamped names for the new backup collections
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    sfx = f"{_BACKUP_SUFFIX}{ts}_nodes_{cur_nodes}_edges_{cur_edges}"
    vbk = f"{nodes_col_name}{sfx}"
    ebk = f"{edges_col_name}{sfx}"

    try:
        # Create vertex backup (document collection)
        db.create_collection(vbk, edge=False)
        # Create edge backup (edge=True is required)
        db.create_collection(ebk, edge=True)

        # Step 4: Atomic AQL copy -- no Python memory roundtrip
        db.aql.execute(f"FOR doc IN {nodes_col_name} INSERT doc INTO {vbk}")
        db.aql.execute(f"FOR doc IN {edges_col_name} INSERT doc INTO {ebk}")

        result["vertex_backup"]   = vbk
        result["edge_backup"]     = ebk
        result["nodes_backed_up"] = cur_nodes
        result["edges_backed_up"] = cur_edges

        print(f"[KGraph-Backup] Backup created: {vbk}  ({cur_nodes} nodes)", flush=True)
        print(f"[KGraph-Backup] Backup created: {ebk}  ({cur_edges} edges)", flush=True)

    except Exception as bkp_err:
        # Roll back any partially created backup collections
        for partial in [vbk, ebk]:
            try:
                if db.has_collection(partial):
                    db.delete_collection(partial)
                    print(f"[KGraph-Backup] Partial backup rolled back: {partial}", flush=True)
            except Exception:
                pass

        # Mandatory: abort sync to protect existing data
        raise RuntimeError(
            f"[KGraph-Backup] BACKUP FAILED -- sync aborted to protect existing data. "
            f"Error: {bkp_err}"
        )

    return result
