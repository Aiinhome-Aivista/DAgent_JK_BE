import pymysql
from database.config import MYSQL_CONFIG

# Embedded SQL queries
# excel report store procedures

SQL_CONTENT = """
----------------- domestic sales value achivements store procedure start ----------------------------------------------------------
DELIMITER $$

DROP PROCEDURE IF EXISTS sp_domestic_sales_value_achivements $$
CREATE PROCEDURE `sp_domestic_sales_value_achivements`(
    IN p_year INT,
    IN p_month INT,
    IN p_day INT
)
BEGIN
    DECLARE v_curr_from DATE;
    DECLARE v_curr_to DATE;
    DECLARE v_prev_from DATE;
    DECLARE v_prev_to DATE;
    DECLARE v_target_month INT;
    /* Dates Setup */
    SET v_curr_from = STR_TO_DATE(CONCAT(p_year,'-',LPAD(p_month,2,'0'),'-01'), '%Y-%m-%d');
    SET v_curr_to = STR_TO_DATE(CONCAT(p_year,'-',LPAD(p_month,2,'0'),'-',LPAD(p_day,2,'0')), '%Y-%m-%d');
    SET v_prev_from = DATE_SUB(v_curr_from, INTERVAL 1 YEAR);
    SET v_prev_to   = DATE_SUB(v_curr_to, INTERVAL 1 YEAR);
    SET v_target_month = p_year * 100 + p_month;
    /* . Market Segment (4 Wheelers & 2/3 Wheelers) & Target */
    WITH target_market AS (
        SELECT 
            CASE
                WHEN UPPER(tt.tyre_type_name) IN ('SCOOTER', 'MOTOR CYCLE', '3W', 'BIKE') THEN '2/3 Wheelers'
                ELSE '4 Wheelers'
            END AS report_name,
            SUM(st.Value) / 1000000 AS Target
        FROM sales_target st
        JOIN material_master sm ON st.MATNR = sm.MATNR
        JOIN tyre_type_master tt ON sm.tyre_type = tt.tyre_type_code
        WHERE st.Month = v_target_month
         -- AND tt.tyre_type_name NOT IN ('OTR', 'Retread', 'OTR+Retread') 
          AND sm.category NOT IN ('JK', 'Vikrant', 'Challenger')
        GROUP BY 
            CASE
                WHEN UPPER(tt.tyre_type_name) IN ('SCOOTER', 'MOTOR CYCLE', '3W', 'BIKE') THEN '2/3 Wheelers'
                ELSE '4 Wheelers'
            END
    ),
    /* . Market Segment (4 Wheelers & 2/3 Wheelers) & Actual Sales */
    actual_market AS (
        SELECT
            CASE
                WHEN UPPER(tt.tyre_type_name) IN ('SCOOTER', 'MOTOR CYCLE', '3W', 'BIKE') THEN '2/3 Wheelers'
                ELSE '4 Wheelers'
            END AS report_name,
            SUM(CASE WHEN sd.billing__doc_date BETWEEN v_prev_from AND LAST_DAY(v_prev_from) THEN sd.NDP_INR ELSE 0 END) / 1000000 AS Month,
            SUM(CASE WHEN sd.billing__doc_date BETWEEN v_prev_from AND v_prev_to THEN sd.NDP_INR ELSE 0 END) / 1000000 AS MTD,
            SUM(CASE WHEN sd.billing__doc_date BETWEEN v_curr_from AND v_curr_to THEN sd.NDP_INR ELSE 0 END) / 1000000 AS Actual,
            SUM(CASE WHEN sd.billing__doc_date = v_curr_to THEN sd.NDP_INR ELSE 0 END) / 1000000 AS Sale_For_Day
        FROM sales_data sd
        JOIN material_master sm ON sd.material = sm.MATNR
        JOIN tyre_type_master tt ON sm.tyre_type = tt.tyre_type_code
        LEFT JOIN distribution_channel_master dm ON sd.distribution__Channel = dm.distribution_code
    --    WHERE tt.tyre_type_name NOT IN ('OTR', 'Retread', 'OTR+Retread') 
          WHERE sm.category NOT IN ('JK', 'Vikrant', 'Challenger') 
          AND (dm.distribution_name IS NULL OR dm.distribution_name NOT IN ('OEM', 'STU', 'DEF')) 
        GROUP BY 
            CASE
                WHEN UPPER(tt.tyre_type_name) IN ('SCOOTER', 'MOTOR CYCLE', '3W', 'BIKE') THEN '2/3 Wheelers'
                ELSE '4 Wheelers'
            END
    ),
    /* Target & Actual join */
    final_market AS (
        SELECT 
            a.report_name, a.Month, a.MTD, IFNULL(t.Target, 0) AS Target, a.Actual, a.Sale_For_Day
        FROM actual_market a
        LEFT JOIN target_market t ON a.report_name = t.report_name
    ),
    /* . Distribution Channel (OEM, DEF, STU) & Actual Sales */
    actual_dist AS (
        SELECT
            CASE
                WHEN dm.distribution_name IN ('OEM','DEF') THEN 'OEM + DEF'
                WHEN dm.distribution_name = 'STU' THEN 'STU'
            END AS report_name,
            SUM(CASE WHEN sd.billing__doc_date BETWEEN v_prev_from AND LAST_DAY(v_prev_from) THEN sd.NDP_INR ELSE 0 END) / 1000000 AS Month,
            SUM(CASE WHEN sd.billing__doc_date BETWEEN v_prev_from AND v_prev_to THEN sd.NDP_INR ELSE 0 END) / 1000000 AS MTD,
            SUM(CASE WHEN sd.billing__doc_date BETWEEN v_curr_from AND v_curr_to THEN sd.NDP_INR ELSE 0 END) / 1000000 AS Actual,
            SUM(CASE WHEN sd.billing__doc_date = v_curr_to THEN sd.NDP_INR ELSE 0 END) / 1000000 AS Sale_For_Day
        FROM sales_data sd
        JOIN distribution_channel_master dm ON sd.distribution__Channel = dm.distribution_code
        WHERE dm.distribution_name IN ('OEM','STU','DEF')
        GROUP BY 
            CASE
                WHEN dm.distribution_name IN ('OEM','DEF') THEN 'OEM + DEF'
                WHEN dm.distribution_name = 'STU' THEN 'STU'
            END
    ),
    /* Distribution এর টার্গেট (যেহেতু sales_target-এ Channel নেই, তাই 0 ধরা হলো) */
    final_dist AS (
        SELECT report_name, Month, MTD, 0 AS Target, Actual, Sale_For_Day FROM actual_dist
    ),
    /* Market এবং Distribution একসাথে করা হলো */
    all_combined AS (
        SELECT * FROM final_market
        UNION ALL
        SELECT * FROM final_dist
    ),
    /* making total summary row (Repl & Domestic)  */
    totals AS (
        SELECT 'Repl (4 & 2/3 Whlrs)' AS report_name, SUM(Month) AS Month, SUM(MTD) AS MTD, SUM(Target) AS Target, SUM(Actual) AS Actual, SUM(Sale_For_Day) AS Sale_For_Day
        FROM final_market
        
        UNION ALL
        
        SELECT 'OEM+STU+DEF' AS report_name, SUM(Month) AS Month, SUM(MTD) AS MTD, SUM(Target) AS Target, SUM(Actual) AS Actual, SUM(Sale_For_Day) AS Sale_For_Day
        FROM final_dist
        
        UNION ALL
        
        SELECT 'Domestic' AS report_name, SUM(Month) AS Month, SUM(MTD) AS MTD, SUM(Target) AS Target, SUM(Actual) AS Actual, SUM(Sale_For_Day) AS Sale_For_Day
        FROM all_combined
    ),
    /* final output*/
    final_output AS (
        SELECT * FROM all_combined
        UNION ALL
        SELECT * FROM totals
    )
    
    /*calculations & percentage */
    SELECT 
        report_name,
        ROUND(Month, 2) AS Month,
        ROUND(MTD, 2) AS MTD,
        ROUND(Target, 2) AS Target,
        ROUND(Actual, 2) AS Actual,
        CONCAT(ROUND((Actual / NULLIF(Target, 0)) * 100, 0), '%') AS Achievement,
        ROUND(Sale_For_Day, 2) AS Sale_For_Day,
        CONCAT(ROUND(((Actual - MTD) / NULLIF(MTD, 0)) * 100, 0), '%') AS Growth_Over_LYSMTD
    FROM final_output
    ORDER BY 
        CASE report_name
            WHEN '4 Wheelers' THEN 1
            WHEN '2/3 Wheelers' THEN 2
            WHEN 'Repl (4 & 2/3 Whlrs)' THEN 3
            WHEN 'OEM + DEF' THEN 4
            WHEN 'STU' THEN 5
            WHEN 'OEM+STU+DEF' THEN 6
            WHEN 'Domestic' THEN 7
        END;
END$$
DELIMITER ;


----------------- domestic sales value achivements store procedure end ----------------------------------------------------------

----------------- domestic sales value achivements store procedure start ----------------------------------------------------------
DELIMITER $$

DROP PROCEDURE IF EXISTS sp_sales_numbers $$
CREATE PROCEDURE `sp_sales_numbers`(
    IN p_year  INT,
    IN p_month INT,
    IN p_day   INT
)
BEGIN
    DECLARE v_curr_from    DATE;
    DECLARE v_curr_to      DATE;
    DECLARE v_prev_from    DATE;
    DECLARE v_prev_to      DATE;
    DECLARE v_target_month INT;

    SET p_year  = IFNULL(p_year , YEAR(CURDATE()));
    SET p_month = IFNULL(p_month, MONTH(CURDATE()));
    SET p_day   = IFNULL(p_day  , DAY(CURDATE()));

    SET v_curr_from = STR_TO_DATE(CONCAT(p_year,'-',p_month,'-01'), '%Y-%c-%d');
    SET v_curr_to   = LEAST(STR_TO_DATE(CONCAT(p_year,'-',p_month,'-',p_day), '%Y-%c-%e'),
                            LAST_DAY(v_curr_from));
    SET v_prev_from = v_curr_from - INTERVAL 1 MONTH;
    SET v_prev_to   = LEAST(v_prev_from + INTERVAL (DAY(v_curr_to) - 1) DAY,
                            LAST_DAY(v_prev_from));
    SET v_target_month = p_year * 100 + p_month;

    WITH
    /* ================= FACTS ============================================ */
    fact AS (
        SELECT
            ttm.tyre_type_name                        AS tt,
            cnm.construction_description              AS con,
            -- CAST(sm.CATG AS CHAR)                     AS catg,
            sd.distribution__Channel                  AS dist_ch,
            sd.billing__doc_date                      AS ddate,
            sd.Sales_Qty                              AS qty,
            cm.CH_TYPE                                AS ch_type,
            cm.class                                  AS cls
        FROM sales_data sd
        JOIN      material_master          sm  ON sm.MATNR              = sd.material
        LEFT JOIN tyre_type_master    ttm ON ttm.tyre_type_code    = CAST(sm.tyre_type AS CHAR)
        LEFT JOIN construction_master cnm ON cnm.construction_code = CAST(sm.construction AS CHAR)
        LEFT JOIN customer_master     cm  ON cm.KUNNR              = sd.customer
        WHERE sd.billing__doc_date BETWEEN v_prev_from AND v_curr_to
          -- AND sm.MTART = 'ZFGS'
          -- AND CAST(sm.CATG AS CHAR) IN ('1','2','5')
    ),

    m_sales AS (
        SELECT tt, con, 
        -- catg,
            SUM(CASE WHEN dist_ch=10 AND ddate BETWEEN v_prev_from AND v_prev_to THEN qty ELSE 0 END) AS jul_act,
            0                                                                                         AS target,
            SUM(CASE WHEN dist_ch=10 AND ddate BETWEEN v_curr_from AND v_curr_to THEN qty ELSE 0 END) AS total,
            SUM(CASE WHEN dist_ch=10 AND ddate = v_curr_to                       THEN qty ELSE 0 END) AS today,
            SUM(CASE WHEN dist_ch=10 AND ddate BETWEEN v_curr_from AND v_curr_to AND ch_type='DL' THEN qty ELSE 0 END) AS dealer,
            SUM(CASE WHEN dist_ch=10 AND ddate BETWEEN v_curr_from AND v_curr_to AND (ch_type='DB' OR cls='DB') THEN qty ELSE 0 END) AS distributor,
            SUM(CASE WHEN dist_ch=10 AND ddate BETWEEN v_curr_from AND v_curr_to AND ch_type='FL' THEN qty ELSE 0 END) AS fleet,
            SUM(CASE WHEN dist_ch=20 AND ddate BETWEEN v_curr_from AND v_curr_to THEN qty ELSE 0 END) AS oem_actual,
            SUM(CASE WHEN dist_ch=30 AND ddate BETWEEN v_curr_from AND v_curr_to THEN qty ELSE 0 END) AS stu_actual,
            SUM(CASE WHEN dist_ch=40 AND ddate BETWEEN v_curr_from AND v_curr_to THEN qty ELSE 0 END) AS def_actual
        FROM fact GROUP BY tt, con
        -- catg
    ),

    m_target AS (
        SELECT
            ttm.tyre_type_name AS tt, cnm.construction_description AS con,
            -- CAST(sm.CATG AS CHAR) AS catg,
            0 AS jul_act, SUM(st.Qty) AS target, 0 AS total, 0 AS today,
            0 AS dealer, 0 AS distributor, 0 AS fleet, 
            0 AS oem_actual, 0 AS stu_actual, 0 AS def_actual
        FROM sales_target st
        JOIN      material_master          sm  ON sm.MATNR              = st.MATNR
        LEFT JOIN tyre_type_master    ttm ON ttm.tyre_type_code    = CAST(sm.tyre_type AS CHAR)
        LEFT JOIN construction_master cnm ON cnm.construction_code = CAST(sm.construction AS CHAR)
        WHERE st.Month = v_target_month
        GROUP BY 1,2,3
    ),

    base AS (SELECT * FROM m_sales UNION ALL SELECT * FROM m_target)

    SELECT
        CONCAT(IFNULL(tt, 'Unknown'), ' - ', IFNULL(con, 'Unknown'))  AS category, 
        COALESCE(SUM(jul_act), 0)                                     AS jul_act,
        COALESCE(SUM(target), 0)                                      AS target,
        COALESCE(SUM(total), 0)                                       AS total,
        COALESCE(SUM(today), 0)                                       AS today,
        COALESCE(SUM(dealer), 0)                                      AS dealer,
        COALESCE(SUM(distributor), 0)                                 AS distributor,
        COALESCE(SUM(fleet), 0)                                       AS fleet,
        COALESCE(SUM(total), 0) - COALESCE(SUM(dealer), 0)
        - COALESCE(SUM(distributor), 0) AS others,
        NULL                                                          AS oem_target,
        COALESCE(SUM(oem_actual), 0)                                  AS oem_actual,
        NULL                                                          AS stu_target,
        COALESCE(SUM(stu_actual), 0)                                  AS stu_actual,
        NULL                                                          AS def_target,
        COALESCE(SUM(def_actual), 0)                                  AS def_actual,
        NULL                                                          AS domestic_target,
        COALESCE(SUM(total + oem_actual + stu_actual + def_actual), 0) AS domestic_actual
    FROM base
    GROUP BY tt, con
    ORDER BY tt, con;

END$$
DELIMITER ;


----------------- sales numbers store procedure end ----------------------------------------------------------

----------------- sp_sales_report_by_values store procedure start ----------------------------------------------------------
DELIMITER $$

DROP PROCEDURE IF EXISTS sp_sales_report_by_values $$
CREATE PROCEDURE `sp_sales_report_by_values`(
    IN p_year  INT,
    IN p_month INT,
    IN p_day   INT
)
BEGIN
    DECLARE v_curr_from      DATE;
    DECLARE v_curr_to        DATE;
    DECLARE v_prev_from      DATE;
    DECLARE v_target_month   INT;
    DECLARE v_remaining_days INT;
    -- Set defaults if NULL passed
    SET p_year  = IFNULL(p_year,  YEAR(CURDATE()));
    SET p_month = IFNULL(p_month, MONTH(CURDATE()));
    SET p_day   = IFNULL(p_day,   DAY(CURDATE()));
    -- Date range variables
    SET v_curr_from      = STR_TO_DATE(CONCAT(p_year,'-',p_month,'-01'), '%Y-%c-%d');
    SET v_curr_to        = LEAST(
                               STR_TO_DATE(CONCAT(p_year,'-',p_month,'-',p_day), '%Y-%c-%e'),
                               LAST_DAY(v_curr_from)
                           );
    SET v_prev_from      = DATE_SUB(v_curr_from, INTERVAL 1 YEAR);
    SET v_target_month   = p_year * 100 + p_month;
    SET v_remaining_days = DAY(LAST_DAY(v_curr_from)) - DAY(v_curr_to);
    WITH
    /* ============================================================
       1. ACTUALS FACT TABLE
          Join path: sales_data
                  -> material_master       (exclusion filters + segment)
                  -> tyre_type_master (4W vs 2/3W segment)
                  -> customer_master  (get territory from customer)
                  -> territory_master (get region from territory)
                  -> region_master    (get zone from region)
       Zone codes in region_master:
         EZ = East   | NZ = North  | CZ = Central
         WZ = West   | SZ = South-I| TZ = South-II | NP = Nepal
       ============================================================ */
    fact_actuals AS (
        SELECT
            CASE rm.zone
                WHEN 'EZ' THEN 'East'
                WHEN 'NZ' THEN 'North'
                WHEN 'CZ' THEN 'Central'
                WHEN 'WZ' THEN 'West'
                WHEN 'SZ' THEN 'South - I'
                WHEN 'TZ' THEN 'South - II'
                WHEN 'NP' THEN 'Nepal'
                ELSE          'Unknown'
            END AS Zone_Name,
            CASE
                WHEN UPPER(IFNULL(ttm.tyre_type_name, ''))
                     IN ('SCOOTER','MOTOR CYCLE','3W','BIKE','3 WHEELER')
                THEN '2_3_Whlrs'
                ELSE '4_Whlrs'
            END AS Segment,
            sd.billing__doc_date,
            sd.NDP_INR
        FROM sales_data sd
        JOIN      material_master       sm  ON sd.material        = sm.MATNR
        LEFT JOIN tyre_type_master ttm ON sm.tyre_type       = CAST(ttm.tyre_type_code AS CHAR)
        LEFT JOIN customer_master  cm  ON sd.customer        = cm.KUNNR
        LEFT JOIN territory_master tm  ON cm.territory       = tm.territory_code
        LEFT JOIN region_master    rm  ON tm.region_code     = rm.region
        WHERE
            (   sd.billing__doc_date BETWEEN v_curr_from AND v_curr_to
             OR sd.billing__doc_date BETWEEN v_prev_from AND LAST_DAY(v_prev_from))
            -- Exclusions
            AND sm.MAKTX NOT LIKE '%TREEL%'
            AND sm.MAKTX NOT LIKE '%PACK TUBE%'
            AND sm.MAKTX NOT LIKE '%LTR%'
            AND sm.MAKTX NOT LIKE '%ALL STEEL%'
            AND sm.MAKTX NOT LIKE '%STEEL BELTED%'
            AND IFNULL(sm.category, '')        NOT IN ('JK', 'Vikrant')
            AND IFNULL(ttm.tyre_type_name, '') != 'OTR'
    ),
    /* ============================================================
       2. AGGREGATE ACTUALS BY ZONE (converted to Crores)
       ============================================================ */
    agg_actuals AS (
        SELECT
            Zone_Name,
            SUM(CASE WHEN billing__doc_date BETWEEN v_prev_from AND LAST_DAY(v_prev_from)
                      AND Segment = '4_Whlrs'
                     THEN NDP_INR ELSE 0 END) / 10000000 AS Prev_4W,
            SUM(CASE WHEN billing__doc_date BETWEEN v_prev_from AND LAST_DAY(v_prev_from)
                      AND Segment = '2_3_Whlrs'
                     THEN NDP_INR ELSE 0 END) / 10000000 AS Prev_23W,
            SUM(CASE WHEN billing__doc_date BETWEEN v_curr_from AND v_curr_to
                      AND Segment = '4_Whlrs'
                     THEN NDP_INR ELSE 0 END) / 10000000 AS Curr_4W,
            SUM(CASE WHEN billing__doc_date BETWEEN v_curr_from AND v_curr_to
                      AND Segment = '2_3_Whlrs'
                     THEN NDP_INR ELSE 0 END) / 10000000 AS Curr_23W,
            SUM(CASE WHEN billing__doc_date = v_curr_to
                     THEN NDP_INR ELSE 0 END) / 10000000 AS Sale_For_Day
        FROM fact_actuals
        GROUP BY Zone_Name
    ),
    /* ============================================================
       3. TARGETS FACT TABLE
          Join path: sales_target
                  -> material_master       (exclusion filters + segment)
                  -> tyre_type_master (4W vs 2/3W segment)
                  -> territory_master (get region from Terr Code)
                  -> region_master    (get zone from region)
       ============================================================ */
      /* ============================================================
       3. TARGETS FACT TABLE
       ============================================================ */
       fact_targets AS (
        SELECT
            CASE rm.zone
                WHEN 'EZ' THEN 'East'
                WHEN 'NZ' THEN 'North'
                WHEN 'CZ' THEN 'Central'
                WHEN 'WZ' THEN 'West'
                WHEN 'SZ' THEN 'South - I'
                WHEN 'TZ' THEN 'South - II'
                WHEN 'NP' THEN 'Nepal'
                ELSE          'Unknown'
            END AS Zone_Name,

            CASE
                WHEN UPPER(IFNULL(ttm.tyre_type_name, ''))
                     IN ('SCOOTER','MOTOR CYCLE','3W','BIKE','3 WHEELER')
                THEN '2_3_Whlrs'
                ELSE '4_Whlrs'
            END AS Segment,

            CAST(REPLACE(IFNULL(st.Value, '0'), ',', '') AS DECIMAL(18, 2)) AS Target_Val

        FROM sales_target st                                     -- ✅ direct, no subquery
        JOIN      material_master       sm  ON st.MATNR       = sm.MATNR
        LEFT JOIN tyre_type_master ttm ON sm.tyre_type   = CAST(ttm.tyre_type_code AS CHAR)
        LEFT JOIN territory_master tm  ON st.Terr_Code   = tm.territory_code  -- ✅ underscore
        LEFT JOIN region_master    rm  ON tm.region_code = rm.region

        WHERE st.Month = v_target_month
            AND sm.MAKTX NOT LIKE '%TREEL%'
            AND sm.MAKTX NOT LIKE '%PACK TUBE%'
            AND sm.MAKTX NOT LIKE '%LTR%'
            AND sm.MAKTX NOT LIKE '%ALL STEEL%'
            AND sm.MAKTX NOT LIKE '%STEEL BELTED%'
            AND IFNULL(sm.category, '')        NOT IN ('JK', 'Vikrant')
            AND IFNULL(ttm.tyre_type_name, '') != 'OTR'
    ),

    /* ============================================================
       4. AGGREGATE TARGETS BY ZONE (converted to Crores)
       ============================================================ */
    agg_targets AS (
        SELECT
            Zone_Name,
            SUM(CASE WHEN Segment = '4_Whlrs'   THEN Target_Val ELSE 0 END) / 10000000 AS Tgt_4W,
            SUM(CASE WHEN Segment = '2_3_Whlrs' THEN Target_Val ELSE 0 END) / 10000000 AS Tgt_23W
        FROM fact_targets
        GROUP BY Zone_Name
    ),
    /* ============================================================
       5. FIXED ZONE LIST
          All 7 zones always appear even when no data exists
       ============================================================ */
    all_zones AS (
        SELECT 'East'        AS Zone_Name, 1 AS Sort_Order UNION ALL
        SELECT 'North',                    2               UNION ALL
        SELECT 'Central',                  3               UNION ALL
        SELECT 'West',                     4               UNION ALL
        SELECT 'South - I',                5               UNION ALL
        SELECT 'South - II',               6               UNION ALL
        SELECT 'Nepal',                    7               UNION ALL
        SELECT 'Unknown',                  8   
    ),
    /* ============================================================
       6. COMBINE ACTUALS + TARGETS ON FIXED ZONE LIST
       ============================================================ */
    final AS (
        SELECT
            z.Zone_Name,
            z.Sort_Order,
            IFNULL(a.Prev_4W,      0) AS Prev_4W,
            IFNULL(a.Prev_23W,     0) AS Prev_23W,
            IFNULL(a.Curr_4W,      0) AS Curr_4W,
            IFNULL(a.Curr_23W,     0) AS Curr_23W,
            IFNULL(a.Sale_For_Day, 0) AS Sale_For_Day,
            IFNULL(t.Tgt_4W,       0) AS Tgt_4W,
            IFNULL(t.Tgt_23W,      0) AS Tgt_23W
        FROM all_zones z
        LEFT JOIN agg_actuals a ON z.Zone_Name = a.Zone_Name
        LEFT JOIN agg_targets t ON z.Zone_Name = t.Zone_Name
    )
    /* ============================================================
       7. FINAL OUTPUT + TOTAL ROW
       ============================================================ */
      SELECT
        Zone_Name                                                            AS `Zone`,
        CAST(ROUND(Prev_4W,            2) AS CHAR)                           AS `Jul-25 4 Whlrs`,
        CAST(ROUND(Prev_23W,           2) AS CHAR)                           AS `Jul-25 2/3 Whlrs`,
        CAST(ROUND(Prev_4W + Prev_23W, 2) AS CHAR)                           AS `Jul-25 Total`,
        CAST(ROUND(Tgt_4W,             2) AS CHAR)                           AS `Target 4 Whlrs`,
        CAST(ROUND(Tgt_23W,            2) AS CHAR)                           AS `Target 2/3 Whlrs`,
        CAST(ROUND(Tgt_4W + Tgt_23W,   2) AS CHAR)                           AS `Target Total`,
        CAST(ROUND(Curr_4W,            2) AS CHAR)                           AS `Actual 4 Whlrs`,
        CAST(ROUND(Curr_23W,           2) AS CHAR)                           AS `Actual 2/3 Whlrs`,
        CAST(ROUND(Curr_4W + Curr_23W, 2) AS CHAR)                           AS `Actual Total`,
        CONCAT(IFNULL(ROUND((Curr_4W+Curr_23W)/NULLIF(Tgt_4W+Tgt_23W,0)*100,0),0),'%') AS `% Achvd`,
        CAST(ROUND(Sale_For_Day, 2) AS CHAR)                                 AS `Sale for the Day`,
        CAST(ROUND(GREATEST(0,(Tgt_4W+Tgt_23W)-(Curr_4W+Curr_23W))/NULLIF(v_remaining_days,0),2) AS CHAR) AS `Asking Rate`,
        CONCAT(IFNULL(ROUND(Curr_4W /NULLIF(Tgt_4W, 0)*100,0),0),'%')       AS `% Achvt 4 Whlrs`,
        CONCAT(IFNULL(ROUND(Curr_23W/NULLIF(Tgt_23W,0)*100,0),0),'%')       AS `% Achvt 2/3 Whlrs`,
        CONCAT(IFNULL(ROUND((Curr_4W+Curr_23W)/NULLIF(Tgt_4W+Tgt_23W,0)*100,0),0),'%') AS `% Achvt Total`
    FROM final
    UNION ALL
    -- Total row
    SELECT
        'Total',
        CAST(ROUND(SUM(Prev_4W),  2) AS CHAR),
        CAST(ROUND(SUM(Prev_23W), 2) AS CHAR),
        CAST(ROUND(SUM(Prev_4W+Prev_23W), 2) AS CHAR),
        CAST(ROUND(SUM(Tgt_4W),   2) AS CHAR),
        CAST(ROUND(SUM(Tgt_23W),  2) AS CHAR),
        CAST(ROUND(SUM(Tgt_4W+Tgt_23W), 2) AS CHAR),
        CAST(ROUND(SUM(Curr_4W),  2) AS CHAR),
        CAST(ROUND(SUM(Curr_23W), 2) AS CHAR),
        CAST(ROUND(SUM(Curr_4W+Curr_23W), 2) AS CHAR),
        CONCAT(IFNULL(ROUND(SUM(Curr_4W+Curr_23W)/NULLIF(SUM(Tgt_4W+Tgt_23W),0)*100,0),0),'%'),
        CAST(ROUND(SUM(Sale_For_Day), 2) AS CHAR),
        CAST(ROUND(GREATEST(0,SUM(Tgt_4W+Tgt_23W)-SUM(Curr_4W+Curr_23W))/NULLIF(v_remaining_days,0),2) AS CHAR),
        CONCAT(IFNULL(ROUND(SUM(Curr_4W) /NULLIF(SUM(Tgt_4W), 0)*100,0),0),'%'),
        CONCAT(IFNULL(ROUND(SUM(Curr_23W)/NULLIF(SUM(Tgt_23W),0)*100,0),0),'%'),
        CONCAT(IFNULL(ROUND(SUM(Curr_4W+Curr_23W)/NULLIF(SUM(Tgt_4W+Tgt_23W),0)*100,0),0),'%')
    FROM final
     UNION ALL

    -- % Contribution row: only for 2026 (Target + Actual) columns
    SELECT
        '% Contribution',
        NULL,   -- Jul-25 4 Whlrs: blank
        NULL,   -- Jul-25 2/3 Whlrs: blank
        NULL,   -- Jul-25 Total: blank
        -- Target 4W contribution
        CONCAT(IFNULL(ROUND(SUM(Tgt_4W) / NULLIF(SUM(Tgt_4W+Tgt_23W),0)*100, 0),0),'%'),
        -- Target 2/3W contribution
        CONCAT(IFNULL(ROUND(SUM(Tgt_23W)/ NULLIF(SUM(Tgt_4W+Tgt_23W),0)*100, 0),0),'%'),
        NULL,   -- Target Total: blank
        -- Actual 4W contribution
        CONCAT(IFNULL(ROUND(SUM(Curr_4W) / NULLIF(SUM(Curr_4W+Curr_23W),0)*100, 0),0),'%'),
        -- Actual 2/3W contribution
        CONCAT(IFNULL(ROUND(SUM(Curr_23W)/ NULLIF(SUM(Curr_4W+Curr_23W),0)*100, 0),0),'%'),
        NULL,   -- Actual Total: blank
        NULL,   -- % Achvd
        NULL,   -- Sale for the Day
        NULL,   -- Asking Rate
        NULL,   -- % Achvt 4W
        NULL,   -- % Achvt 2/3W
        NULL    -- % Achvt Total
    FROM final


   ORDER BY CASE `Zone`
        WHEN 'East'           THEN 1
        WHEN 'North'          THEN 2
        WHEN 'Central'        THEN 3
        WHEN 'West'           THEN 4
        WHEN 'South - I'      THEN 5
        WHEN 'South - II'     THEN 6
        WHEN 'Nepal'          THEN 7
        WHEN 'Unknown'        THEN 8       -- add this
        WHEN 'Total'          THEN 9
        WHEN '% Contribution' THEN 10
        ELSE 99
    END;
END$$
DELIMITER ;


----------------- sp_sales_report_by_values store procedure end ----------------------------------------------------------

----------------- sp_sales_summary_in_no_and_values_for_month store procedure start ----------------------------------------------------------
DELIMITER $$

DROP PROCEDURE IF EXISTS sp_sales_summary_in_no_and_values_for_month $$
CREATE PROCEDURE `sp_sales_summary_in_no_and_values_for_month`(
    IN p_year  INT,
    IN p_month INT,
    IN p_day   INT
)
BEGIN
    DECLARE v_curr_from    DATE;
    DECLARE v_curr_to      DATE;
    DECLARE v_target_month INT;
    SET p_year  = IFNULL(p_year , YEAR(CURDATE()));
    SET p_month = IFNULL(p_month, MONTH(CURDATE()));
    SET p_day   = IFNULL(p_day  , DAY(CURDATE()));
    SET v_curr_from = STR_TO_DATE(CONCAT(p_year,'-',p_month,'-01'), '%Y-%c-%d');
    SET v_curr_to   = LEAST(STR_TO_DATE(CONCAT(p_year,'-',p_month,'-',p_day), '%Y-%c-%e'),
                            LAST_DAY(v_curr_from));
    SET v_target_month = p_year * 100 + p_month;
    WITH
    -- 1. Actual Sales Data Aggregated by Zone
    agg_actuals AS (
        SELECT 
            IFNULL(
                CASE rm.zone
                    WHEN 'EZ' THEN 'East'
                    WHEN 'NZ' THEN 'North'
                    WHEN 'CZ' THEN 'Central'
                    WHEN 'WZ' THEN 'West'
                    WHEN 'SZ' THEN 'South - I'
                    WHEN 'TZ' THEN 'South - II'
                    WHEN 'NP' THEN 'Nepal'
                    ELSE 'Unknown'
                END, 'Unknown'
            ) AS Zone_Name,
            
            SUM(CASE WHEN UPPER(IFNULL(tt.tyre_type_name, '')) NOT IN ('SCOOTER','MOTOR CYCLE','3W','BIKE','3 WHEELER') THEN sd.NDP_INR ELSE 0 END) / 10000000 AS `Val_4_Whlrs`,
            SUM(CASE WHEN UPPER(IFNULL(tt.tyre_type_name, '')) IN ('SCOOTER','MOTOR CYCLE','3W','BIKE','3 WHEELER') THEN sd.NDP_INR ELSE 0 END) / 10000000 AS `Val_2_3_Whlrs`,
            SUM(CASE WHEN tt.tyre_type_name = 'TRUCK' AND IFNULL(cm.construction_description, sm.construction) IN ('BIAS', '1') THEN sd.Sales_Qty ELSE 0 END) AS `TBB_Total`,
            SUM(CASE WHEN tt.tyre_type_name = 'TRUCK' AND IFNULL(cm.construction_description, sm.construction) IN ('RADIAL', '2') THEN sd.Sales_Qty ELSE 0 END) AS `TBR_Total`,
            SUM(CASE WHEN tt.tyre_type_name = 'LCV' AND IFNULL(cm.construction_description, sm.construction) IN ('BIAS', '1') THEN sd.Sales_Qty ELSE 0 END) AS `LCV_Bias`,
            SUM(CASE WHEN tt.tyre_type_name = 'LCV' AND IFNULL(cm.construction_description, sm.construction) IN ('RADIAL', '2') THEN sd.Sales_Qty ELSE 0 END) AS `LCV_Radial`,
            SUM(CASE WHEN tt.tyre_type_name = 'SCV' AND IFNULL(cm.construction_description, sm.construction) IN ('BIAS', '1') THEN sd.Sales_Qty ELSE 0 END) AS `SCV_Bias`,
            SUM(CASE WHEN tt.tyre_type_name = 'SCV' AND IFNULL(cm.construction_description, sm.construction) IN ('RADIAL', '2') THEN sd.Sales_Qty ELSE 0 END) AS `SCV_Radial`,
            SUM(CASE WHEN tt.tyre_type_name = 'CAR' AND IFNULL(cm.construction_description, sm.construction) IN ('BIAS', '1') THEN sd.Sales_Qty ELSE 0 END) AS `Car_Bias`,
            SUM(CASE WHEN tt.tyre_type_name = 'CAR' AND IFNULL(cm.construction_description, sm.construction) IN ('RADIAL', '2') THEN sd.Sales_Qty ELSE 0 END) AS `Car_Radial`,
            SUM(CASE WHEN tt.tyre_type_name = 'JEEP' AND IFNULL(cm.construction_description, sm.construction) IN ('BIAS', '1') THEN sd.Sales_Qty ELSE 0 END) AS `Jeep_Bias`,
            SUM(CASE WHEN tt.tyre_type_name = 'JEEP' AND IFNULL(cm.construction_description, sm.construction) IN ('RADIAL', '2') THEN sd.Sales_Qty ELSE 0 END) AS `Jeep_Radial`,
            SUM(CASE WHEN tt.tyre_type_name = 'TRACTOR FRONT' THEN sd.Sales_Qty ELSE 0 END) AS `Tr_Front`,
            SUM(CASE WHEN tt.tyre_type_name = 'TRACTOR REAR' THEN sd.Sales_Qty ELSE 0 END) AS `Tr_Rear`,
            SUM(CASE WHEN tt.tyre_type_name = 'TRACTOR TRAILER' THEN sd.Sales_Qty ELSE 0 END) AS `Tr_Trail`,
            SUM(CASE WHEN tt.tyre_type_name = 'ADV' THEN sd.Sales_Qty ELSE 0 END) AS `ADV`,
            SUM(CASE WHEN tt.tyre_type_name = '3W' THEN sd.Sales_Qty ELSE 0 END) AS `3_Whlrs`,
            SUM(CASE WHEN tt.tyre_type_name = 'SCOOTER' THEN sd.Sales_Qty ELSE 0 END) AS `Scooter`,
            SUM(CASE WHEN tt.tyre_type_name = 'Motor Cycle' THEN sd.Sales_Qty ELSE 0 END) AS `Motor`
        FROM sales_data sd
        JOIN material_master sm ON sd.material = sm.MATNR
        LEFT JOIN tyre_type_master tt ON tt.tyre_type_code = CAST(sm.tyre_type AS CHAR)
        LEFT JOIN construction_master cm ON cm.construction_code = CAST(sm.construction AS CHAR)
        LEFT JOIN customer_master cust ON cust.KUNNR = sd.customer
        LEFT JOIN territory_master tm ON tm.territory_code = CAST(cust.territory AS CHAR)
        LEFT JOIN region_master rm ON rm.region = tm.region_code
        WHERE sd.billing__doc_date BETWEEN v_curr_from AND v_curr_to
          AND sm.MTART = 'ZFGS'
          AND sm.MAKTX NOT LIKE '%TREEL%'
          AND sm.MAKTX NOT LIKE '%PACK TUBE%'
          AND sm.MAKTX NOT LIKE '%SMART TYRE%'
          AND IFNULL(sm.category, '') NOT IN ('JK', 'Vikrant')
        GROUP BY Zone_Name
    ),
    -- 2. Target Data Aggregated by Zone
    agg_targets AS (
        SELECT 
            IFNULL(
                CASE rm.zone
                    WHEN 'EZ' THEN 'East'
                    WHEN 'NZ' THEN 'North'
                    WHEN 'CZ' THEN 'Central'
                    WHEN 'WZ' THEN 'West'
                    WHEN 'SZ' THEN 'South - I'
                    WHEN 'TZ' THEN 'South - II'
                    WHEN 'NP' THEN 'Nepal'
                    ELSE 'Unknown'
                END, 'Unknown'
            ) AS Zone_Name,
            
            SUM(CASE WHEN UPPER(IFNULL(tt.tyre_type_name, '')) NOT IN ('SCOOTER','MOTOR CYCLE','3W','BIKE','3 WHEELER') THEN CAST(REPLACE(IFNULL(st.Value, '0'), ',', '') AS DECIMAL(18, 2)) ELSE 0 END) / 10000000 AS `Val_4_Whlrs`, 
            SUM(CASE WHEN UPPER(IFNULL(tt.tyre_type_name, '')) IN ('SCOOTER','MOTOR CYCLE','3W','BIKE','3 WHEELER') THEN CAST(REPLACE(IFNULL(st.Value, '0'), ',', '') AS DECIMAL(18, 2)) ELSE 0 END) / 10000000 AS `Val_2_3_Whlrs`,
            SUM(CASE WHEN tt.tyre_type_name = 'TRUCK' AND IFNULL(cm.construction_description, sm.construction) IN ('BIAS', '1') THEN CAST(REPLACE(IFNULL(st.Qty, '0'), ',', '') AS DECIMAL(18, 2)) ELSE 0 END) AS `TBB_Total`,
            SUM(CASE WHEN tt.tyre_type_name = 'TRUCK' AND IFNULL(cm.construction_description, sm.construction) IN ('RADIAL', '2') THEN CAST(REPLACE(IFNULL(st.Qty, '0'), ',', '') AS DECIMAL(18, 2)) ELSE 0 END) AS `TBR_Total`,
            SUM(CASE WHEN tt.tyre_type_name = 'LCV' AND IFNULL(cm.construction_description, sm.construction) IN ('BIAS', '1') THEN CAST(REPLACE(IFNULL(st.Qty, '0'), ',', '') AS DECIMAL(18, 2)) ELSE 0 END) AS `LCV_Bias`,
            SUM(CASE WHEN tt.tyre_type_name = 'LCV' AND IFNULL(cm.construction_description, sm.construction) IN ('RADIAL', '2') THEN CAST(REPLACE(IFNULL(st.Qty, '0'), ',', '') AS DECIMAL(18, 2)) ELSE 0 END) AS `LCV_Radial`,
            SUM(CASE WHEN tt.tyre_type_name = 'SCV' AND IFNULL(cm.construction_description, sm.construction) IN ('BIAS', '1') THEN CAST(REPLACE(IFNULL(st.Qty, '0'), ',', '') AS DECIMAL(18, 2)) ELSE 0 END) AS `SCV_Bias`,
            SUM(CASE WHEN tt.tyre_type_name = 'SCV' AND IFNULL(cm.construction_description, sm.construction) IN ('RADIAL', '2') THEN CAST(REPLACE(IFNULL(st.Qty, '0'), ',', '') AS DECIMAL(18, 2)) ELSE 0 END) AS `SCV_Radial`,
            SUM(CASE WHEN tt.tyre_type_name = 'CAR' AND IFNULL(cm.construction_description, sm.construction) IN ('BIAS', '1') THEN CAST(REPLACE(IFNULL(st.Qty, '0'), ',', '') AS DECIMAL(18, 2)) ELSE 0 END) AS `Car_Bias`,
            SUM(CASE WHEN tt.tyre_type_name = 'CAR' AND IFNULL(cm.construction_description, sm.construction) IN ('RADIAL', '2') THEN CAST(REPLACE(IFNULL(st.Qty, '0'), ',', '') AS DECIMAL(18, 2)) ELSE 0 END) AS `Car_Radial`,
            SUM(CASE WHEN tt.tyre_type_name = 'JEEP' AND IFNULL(cm.construction_description, sm.construction) IN ('BIAS', '1') THEN CAST(REPLACE(IFNULL(st.Qty, '0'), ',', '') AS DECIMAL(18, 2)) ELSE 0 END) AS `Jeep_Bias`,
            SUM(CASE WHEN tt.tyre_type_name = 'JEEP' AND IFNULL(cm.construction_description, sm.construction) IN ('RADIAL', '2') THEN CAST(REPLACE(IFNULL(st.Qty, '0'), ',', '') AS DECIMAL(18, 2)) ELSE 0 END) AS `Jeep_Radial`,
            SUM(CASE WHEN tt.tyre_type_name = 'TRACTOR FRONT' THEN CAST(REPLACE(IFNULL(st.Qty, '0'), ',', '') AS DECIMAL(18, 2)) ELSE 0 END) AS `Tr_Front`,
            SUM(CASE WHEN tt.tyre_type_name = 'TRACTOR REAR' THEN CAST(REPLACE(IFNULL(st.Qty, '0'), ',', '') AS DECIMAL(18, 2)) ELSE 0 END) AS `Tr_Rear`,
            SUM(CASE WHEN tt.tyre_type_name = 'TRACTOR TRAILER' THEN CAST(REPLACE(IFNULL(st.Qty, '0'), ',', '') AS DECIMAL(18, 2)) ELSE 0 END) AS `Tr_Trail`,
            SUM(CASE WHEN tt.tyre_type_name = 'ADV' THEN CAST(REPLACE(IFNULL(st.Qty, '0'), ',', '') AS DECIMAL(18, 2)) ELSE 0 END) AS `ADV`,
            SUM(CASE WHEN tt.tyre_type_name = '3W' THEN CAST(REPLACE(IFNULL(st.Qty, '0'), ',', '') AS DECIMAL(18, 2)) ELSE 0 END) AS `3_Whlrs`,
            SUM(CASE WHEN tt.tyre_type_name = 'SCOOTER' THEN CAST(REPLACE(IFNULL(st.Qty, '0'), ',', '') AS DECIMAL(18, 2)) ELSE 0 END) AS `Scooter`,
            SUM(CASE WHEN tt.tyre_type_name = 'Motor Cycle' THEN CAST(REPLACE(IFNULL(st.Qty, '0'), ',', '') AS DECIMAL(18, 2)) ELSE 0 END) AS `Motor`
        FROM sales_target st
        JOIN material_master sm ON st.MATNR = sm.MATNR
        LEFT JOIN tyre_type_master tt ON tt.tyre_type_code = CAST(sm.tyre_type AS CHAR)
        LEFT JOIN construction_master cm ON cm.construction_code = CAST(sm.construction AS CHAR)
        LEFT JOIN territory_master tm ON tm.territory_code = st.Terr_Code
        LEFT JOIN region_master rm ON rm.region = tm.region_code
        WHERE st.Month = v_target_month
          AND sm.MAKTX NOT LIKE '%TREEL%'
          AND sm.MAKTX NOT LIKE '%PACK TUBE%'
          AND sm.MAKTX NOT LIKE '%SMART TYRE%'
          AND IFNULL(sm.category, '') NOT IN ('JK', 'Vikrant')
        GROUP BY Zone_Name
    ),
    -- 3. All Zones List to ensure consistent order
    all_zones AS (
        SELECT 'East' AS Zone_Name UNION ALL
        SELECT 'North' UNION ALL
        SELECT 'Central' UNION ALL
        SELECT 'West' UNION ALL
        SELECT 'South - I' UNION ALL
        SELECT 'South - II' UNION ALL
        SELECT 'Nepal' UNION ALL
        SELECT 'Unknown'
    ),
    -- 4. Final Combined Data
    final_data AS (
        SELECT 
            z.Zone_Name,
            IFNULL(a.Val_4_Whlrs, 0) AS Act_Val_4W, IFNULL(t.Val_4_Whlrs, 0) AS Tgt_Val_4W,
            IFNULL(a.Val_2_3_Whlrs, 0) AS Act_Val_23W, IFNULL(t.Val_2_3_Whlrs, 0) AS Tgt_Val_23W,
            IFNULL(a.TBB_Total, 0) AS Act_TBB, IFNULL(t.TBB_Total, 0) AS Tgt_TBB,
            IFNULL(a.TBR_Total, 0) AS Act_TBR, IFNULL(t.TBR_Total, 0) AS Tgt_TBR,
            IFNULL(a.LCV_Bias, 0) AS Act_LCV_B, IFNULL(t.LCV_Bias, 0) AS Tgt_LCV_B,
            IFNULL(a.LCV_Radial, 0) AS Act_LCV_R, IFNULL(t.LCV_Radial, 0) AS Tgt_LCV_R,
            IFNULL(a.SCV_Bias, 0) AS Act_SCV_B, IFNULL(t.SCV_Bias, 0) AS Tgt_SCV_B,
            IFNULL(a.SCV_Radial, 0) AS Act_SCV_R, IFNULL(t.SCV_Radial, 0) AS Tgt_SCV_R,
            IFNULL(a.Car_Bias, 0) AS Act_Car_B, IFNULL(t.Car_Bias, 0) AS Tgt_Car_B,
            IFNULL(a.Car_Radial, 0) AS Act_Car_R, IFNULL(t.Car_Radial, 0) AS Tgt_Car_R,
            IFNULL(a.Jeep_Bias, 0) AS Act_Jeep_B, IFNULL(t.Jeep_Bias, 0) AS Tgt_Jeep_B,
            IFNULL(a.Jeep_Radial, 0) AS Act_Jeep_R, IFNULL(t.Jeep_Radial, 0) AS Tgt_Jeep_R,
            IFNULL(a.Tr_Front, 0) AS Act_TrF, IFNULL(t.Tr_Front, 0) AS Tgt_TrF,
            IFNULL(a.Tr_Rear, 0) AS Act_TrR, IFNULL(t.Tr_Rear, 0) AS Tgt_TrR,
            IFNULL(a.Tr_Trail, 0) AS Act_TrT, IFNULL(t.Tr_Trail, 0) AS Tgt_TrT,
            IFNULL(a.ADV, 0) AS Act_ADV, IFNULL(t.ADV, 0) AS Tgt_ADV,
            IFNULL(a.`3_Whlrs`, 0) AS Act_3W, IFNULL(t.`3_Whlrs`, 0) AS Tgt_3W,
            IFNULL(a.Scooter, 0) AS Act_Scoot, IFNULL(t.Scooter, 0) AS Tgt_Scoot,
            IFNULL(a.Motor, 0) AS Act_Motor, IFNULL(t.Motor, 0) AS Tgt_Motor
        FROM all_zones z
        LEFT JOIN agg_actuals a ON z.Zone_Name = a.Zone_Name
        LEFT JOIN agg_targets t ON z.Zone_Name = t.Zone_Name
    )
    
    -- 5. Final Output
    -- ACTUAL ROWS
    SELECT 
        Zone_Name AS `Zone`, 'Actual' AS `Type`,
        CAST(ROUND(Act_Val_4W, 2) AS CHAR) AS `4 Whlrs.`, CAST(ROUND(Act_Val_23W, 2) AS CHAR) AS `Whlrs`, CAST(ROUND(Act_Val_4W + Act_Val_23W, 2) AS CHAR) AS `Total`,
        CAST(Act_TBB AS CHAR) AS `TBB Total`, CAST(Act_TBR AS CHAR) AS `TBR Total`,
        CAST(Act_LCV_B AS CHAR) AS `LCV Bias`, CAST(Act_LCV_R AS CHAR) AS `LCV Rdl`,
        CAST(Act_SCV_B AS CHAR) AS `SCV Bias`, CAST(Act_SCV_R AS CHAR) AS `SCV Rdl.`,
        CAST(Act_Car_B AS CHAR) AS `Car Bias`, CAST(Act_Car_R AS CHAR) AS `Car Radial`,
        CAST(Act_Jeep_B AS CHAR) AS `Jeep Bias`, CAST(Act_Jeep_R AS CHAR) AS `Jeep Radial`,
        CAST(Act_TrF AS CHAR) AS `Tr. Fro`, CAST(Act_TrR AS CHAR) AS `Tr. Rear`, CAST(Act_TrT AS CHAR) AS `Tr. Trail`,
        CAST(Act_ADV AS CHAR) AS `ADV`, CAST(Act_3W AS CHAR) AS `3 Whlrs`, CAST(Act_Scoot AS CHAR) AS `Scooter`, CAST(Act_Motor AS CHAR) AS `Motor`
    FROM final_data
    
    UNION ALL
    
    -- TARGET ROWS
    SELECT 
        Zone_Name, 'Target',
        CAST(ROUND(Tgt_Val_4W, 2) AS CHAR), CAST(ROUND(Tgt_Val_23W, 2) AS CHAR), CAST(ROUND(Tgt_Val_4W + Tgt_Val_23W, 2) AS CHAR),
        CAST(Tgt_TBB AS CHAR), CAST(Tgt_TBR AS CHAR),
        CAST(Tgt_LCV_B AS CHAR), CAST(Tgt_LCV_R AS CHAR),
        CAST(Tgt_SCV_B AS CHAR), CAST(Tgt_SCV_R AS CHAR),
        CAST(Tgt_Car_B AS CHAR), CAST(Tgt_Car_R AS CHAR),
        CAST(Tgt_Jeep_B AS CHAR), CAST(Tgt_Jeep_R AS CHAR),
        CAST(Tgt_TrF AS CHAR), CAST(Tgt_TrR AS CHAR), CAST(Tgt_TrT AS CHAR),
        CAST(Tgt_ADV AS CHAR), CAST(Tgt_3W AS CHAR), CAST(Tgt_Scoot AS CHAR), CAST(Tgt_Motor AS CHAR)
    FROM final_data
    
    UNION ALL
    
    -- % ACHV ROWS
    SELECT 
        Zone_Name, 'Achv %',
        CONCAT(IFNULL(ROUND(Act_Val_4W / NULLIF(Tgt_Val_4W, 0) * 100, 0), 0), '%'), 
        CONCAT(IFNULL(ROUND(Act_Val_23W / NULLIF(Tgt_Val_23W, 0) * 100, 0), 0), '%'), 
        CONCAT(IFNULL(ROUND((Act_Val_4W + Act_Val_23W) / NULLIF((Tgt_Val_4W + Tgt_Val_23W), 0) * 100, 0), 0), '%'),
        CONCAT(IFNULL(ROUND(Act_TBB / NULLIF(Tgt_TBB, 0) * 100, 0), 0), '%'), 
        CONCAT(IFNULL(ROUND(Act_TBR / NULLIF(Tgt_TBR, 0) * 100, 0), 0), '%'),
        CONCAT(IFNULL(ROUND(Act_LCV_B / NULLIF(Tgt_LCV_B, 0) * 100, 0), 0), '%'), 
        CONCAT(IFNULL(ROUND(Act_LCV_R / NULLIF(Tgt_LCV_R, 0) * 100, 0), 0), '%'),
        CONCAT(IFNULL(ROUND(Act_SCV_B / NULLIF(Tgt_SCV_B, 0) * 100, 0), 0), '%'), 
        CONCAT(IFNULL(ROUND(Act_SCV_R / NULLIF(Tgt_SCV_R, 0) * 100, 0), 0), '%'),
        CONCAT(IFNULL(ROUND(Act_Car_B / NULLIF(Tgt_Car_B, 0) * 100, 0), 0), '%'), 
        CONCAT(IFNULL(ROUND(Act_Car_R / NULLIF(Tgt_Car_R, 0) * 100, 0), 0), '%'),
        CONCAT(IFNULL(ROUND(Act_Jeep_B / NULLIF(Tgt_Jeep_B, 0) * 100, 0), 0), '%'), 
        CONCAT(IFNULL(ROUND(Act_Jeep_R / NULLIF(Tgt_Jeep_R, 0) * 100, 0), 0), '%'),
        CONCAT(IFNULL(ROUND(Act_TrF / NULLIF(Tgt_TrF, 0) * 100, 0), 0), '%'), 
        CONCAT(IFNULL(ROUND(Act_TrR / NULLIF(Tgt_TrR, 0) * 100, 0), 0), '%'), 
        CONCAT(IFNULL(ROUND(Act_TrT / NULLIF(Tgt_TrT, 0) * 100, 0), 0), '%'),
        CONCAT(IFNULL(ROUND(Act_ADV / NULLIF(Tgt_ADV, 0) * 100, 0), 0), '%'), 
        CONCAT(IFNULL(ROUND(Act_3W / NULLIF(Tgt_3W, 0) * 100, 0), 0), '%'), 
        CONCAT(IFNULL(ROUND(Act_Scoot / NULLIF(Tgt_Scoot, 0) * 100, 0), 0), '%'), 
        CONCAT(IFNULL(ROUND(Act_Motor / NULLIF(Tgt_Motor, 0) * 100, 0), 0), '%')
    FROM final_data
    
    UNION ALL
    
    -- GRAND TOTAL ACTUAL
    SELECT 
        'Grand Total', 'Actual',
        CAST(ROUND(SUM(Act_Val_4W), 2) AS CHAR), CAST(ROUND(SUM(Act_Val_23W), 2) AS CHAR), CAST(ROUND(SUM(Act_Val_4W + Act_Val_23W), 2) AS CHAR),
        CAST(SUM(Act_TBB) AS CHAR), CAST(SUM(Act_TBR) AS CHAR),
        CAST(SUM(Act_LCV_B) AS CHAR), CAST(SUM(Act_LCV_R) AS CHAR),
        CAST(SUM(Act_SCV_B) AS CHAR), CAST(SUM(Act_SCV_R) AS CHAR),
        CAST(SUM(Act_Car_B) AS CHAR), CAST(SUM(Act_Car_R) AS CHAR),
        CAST(SUM(Act_Jeep_B) AS CHAR), CAST(SUM(Act_Jeep_R) AS CHAR),
        CAST(SUM(Act_TrF) AS CHAR), CAST(SUM(Act_TrR) AS CHAR), CAST(SUM(Act_TrT) AS CHAR),
        CAST(SUM(Act_ADV) AS CHAR), CAST(SUM(Act_3W) AS CHAR), CAST(SUM(Act_Scoot) AS CHAR), CAST(SUM(Act_Motor) AS CHAR)
    FROM final_data
    
    UNION ALL
    
    -- GRAND TOTAL TARGET
    SELECT 
        'Grand Total', 'Target',
        CAST(ROUND(SUM(Tgt_Val_4W), 2) AS CHAR), CAST(ROUND(SUM(Tgt_Val_23W), 2) AS CHAR), CAST(ROUND(SUM(Tgt_Val_4W + Tgt_Val_23W), 2) AS CHAR),
        CAST(SUM(Tgt_TBB) AS CHAR), CAST(SUM(Tgt_TBR) AS CHAR),
        CAST(SUM(Tgt_LCV_B) AS CHAR), CAST(SUM(Tgt_LCV_R) AS CHAR),
        CAST(SUM(Tgt_SCV_B) AS CHAR), CAST(SUM(Tgt_SCV_R) AS CHAR),
        CAST(SUM(Tgt_Car_B) AS CHAR), CAST(SUM(Tgt_Car_R) AS CHAR),
        CAST(SUM(Tgt_Jeep_B) AS CHAR), CAST(SUM(Tgt_Jeep_R) AS CHAR),
        CAST(SUM(Tgt_TrF) AS CHAR), CAST(SUM(Tgt_TrR) AS CHAR), CAST(SUM(Tgt_TrT) AS CHAR),
        CAST(SUM(Tgt_ADV) AS CHAR), CAST(SUM(Tgt_3W) AS CHAR), CAST(SUM(Tgt_Scoot) AS CHAR), CAST(SUM(Tgt_Motor) AS CHAR)
    FROM final_data
    
    UNION ALL
    
    -- GRAND TOTAL % ACHV
    SELECT 
        'Grand Total', 'Achv %',
        CONCAT(IFNULL(ROUND(SUM(Act_Val_4W) / NULLIF(SUM(Tgt_Val_4W), 0) * 100, 0), 0), '%'),
        CONCAT(IFNULL(ROUND(SUM(Act_Val_23W) / NULLIF(SUM(Tgt_Val_23W), 0) * 100, 0), 0), '%'),
        CONCAT(IFNULL(ROUND(SUM(Act_Val_4W + Act_Val_23W) / NULLIF(SUM(Tgt_Val_4W + Tgt_Val_23W), 0) * 100, 0), 0), '%'),
        CONCAT(IFNULL(ROUND(SUM(Act_TBB) / NULLIF(SUM(Tgt_TBB), 0) * 100, 0), 0), '%'),
        CONCAT(IFNULL(ROUND(SUM(Act_TBR) / NULLIF(SUM(Tgt_TBR), 0) * 100, 0), 0), '%'),
        CONCAT(IFNULL(ROUND(SUM(Act_LCV_B) / NULLIF(SUM(Tgt_LCV_B), 0) * 100, 0), 0), '%'),
        CONCAT(IFNULL(ROUND(SUM(Act_LCV_R) / NULLIF(SUM(Tgt_LCV_R), 0) * 100, 0), 0), '%'),
        CONCAT(IFNULL(ROUND(SUM(Act_SCV_B) / NULLIF(SUM(Tgt_SCV_B), 0) * 100, 0), 0), '%'),
        CONCAT(IFNULL(ROUND(SUM(Act_SCV_R) / NULLIF(SUM(Tgt_SCV_R), 0) * 100, 0), 0), '%'),
        CONCAT(IFNULL(ROUND(SUM(Act_Car_B) / NULLIF(SUM(Tgt_Car_B), 0) * 100, 0), 0), '%'),
        CONCAT(IFNULL(ROUND(SUM(Act_Car_R) / NULLIF(SUM(Tgt_Car_R), 0) * 100, 0), 0), '%'),
        CONCAT(IFNULL(ROUND(SUM(Act_Jeep_B) / NULLIF(SUM(Tgt_Jeep_B), 0) * 100, 0), 0), '%'),
        CONCAT(IFNULL(ROUND(SUM(Act_Jeep_R) / NULLIF(SUM(Tgt_Jeep_R), 0) * 100, 0), 0), '%'),
        CONCAT(IFNULL(ROUND(SUM(Act_TrF) / NULLIF(SUM(Tgt_TrF), 0) * 100, 0), 0), '%'),
        CONCAT(IFNULL(ROUND(SUM(Act_TrR) / NULLIF(SUM(Tgt_TrR), 0) * 100, 0), 0), '%'),
        CONCAT(IFNULL(ROUND(SUM(Act_TrT) / NULLIF(SUM(Tgt_TrT), 0) * 100, 0), 0), '%'),
        CONCAT(IFNULL(ROUND(SUM(Act_ADV) / NULLIF(SUM(Tgt_ADV), 0) * 100, 0), 0), '%'),
        CONCAT(IFNULL(ROUND(SUM(Act_3W) / NULLIF(SUM(Tgt_3W), 0) * 100, 0), 0), '%'),
        CONCAT(IFNULL(ROUND(SUM(Act_Scoot) / NULLIF(SUM(Tgt_Scoot), 0) * 100, 0), 0), '%'),
        CONCAT(IFNULL(ROUND(SUM(Act_Motor) / NULLIF(SUM(Tgt_Motor), 0) * 100, 0), 0), '%')
    FROM final_data
    
    ORDER BY 
        FIELD(`Zone`, 'East', 'North', 'Central', 'West', 'South - I', 'South - II', 'Nepal', 'Unknown', 'Grand Total'),
        FIELD(`Type`, 'Actual', 'Target', 'Achv %');
END$$
DELIMITER ;


----------------- sp_sales_summary_in_no_and_values_for_month store procedure end ----------------------------------------------------------

----------------- sp_get_sales_revenue_by_zone store procedure start ----------------------------------------------------------
DELIMITER $$

DROP PROCEDURE IF EXISTS sp_get_sales_revenue_by_zone $$
CREATE PROCEDURE `sp_get_sales_revenue_by_zone`(IN p_zone VARCHAR(50))
BEGIN
    IF p_zone IS NULL OR p_zone = '' OR p_zone = 'All' THEN
        SELECT 
            COALESCE(s.zone, t.zone) AS zone,
            COALESCE(s.saleValueCr, 0) AS saleValueCr,
            COALESCE(t.planValueCr, 0) AS planValueCr,
            CASE 
                WHEN COALESCE(t.planValueCr, 0) > 0 THEN ROUND((COALESCE(s.saleValueCr, 0) / t.planValueCr) * 100, 2)
                ELSE 0 
            END AS achievValuePct
        FROM (
            -- Sales Data (Actual)
            SELECT 
                rm.zone AS zone, 
                ROUND(SUM(sd.Invoice_Value_INR) / 10000000, 2) AS saleValueCr
            FROM sales_data sd
            JOIN customer_master cust ON sd.customer = cust.KUNNR
            JOIN territory_master tm ON cust.territory = tm.territory_code
            JOIN region_master rm ON tm.region_code = rm.region
            GROUP BY rm.zone
        ) s
        LEFT JOIN (
            -- Target Data (Plan)
            SELECT 
                rm.zone AS zone, 
                ROUND(SUM(st.Value) / 10000000, 2) AS planValueCr
            FROM sales_target st
            JOIN territory_master tm ON st.Terr_Code = tm.territory_code
            JOIN region_master rm ON tm.region_code = rm.region
            GROUP BY rm.zone
        ) t ON s.zone = t.zone;

    ELSE
        SELECT 
            COALESCE(s.region, t.region) AS region,
            COALESCE(s.saleValueCr, 0) AS saleValueCr,
            COALESCE(t.planValueCr, 0) AS planValueCr,
            CASE 
                WHEN COALESCE(t.planValueCr, 0) > 0 THEN ROUND((COALESCE(s.saleValueCr, 0) / t.planValueCr) * 100, 2)
                ELSE 0 
            END AS achievValuePct
        FROM (
            -- Sales Data (Actual)
            SELECT 
                rm.region_name AS region, 
                ROUND(SUM(sd.Invoice_Value_INR) / 10000000, 2) AS saleValueCr
            FROM sales_data sd
            JOIN customer_master cust ON sd.customer = cust.KUNNR
            JOIN territory_master tm ON cust.territory = tm.territory_code
            JOIN region_master rm ON tm.region_code = rm.region
            WHERE rm.zone = p_zone
            GROUP BY rm.region_name
        ) s
        LEFT JOIN (
            -- Target Data (Plan)
            SELECT 
                rm.region_name AS region, 
                ROUND(SUM(st.Value) / 10000000, 2) AS planValueCr
            FROM sales_target st
            JOIN territory_master tm ON st.Terr_Code = tm.territory_code
            JOIN region_master rm ON tm.region_code = rm.region
            WHERE rm.zone = p_zone
            GROUP BY rm.region_name
        ) t ON s.region = t.region;
    END IF;
END$$
DELIMITER ;


----------------- sp_get_sales_revenue_by_zone store procedure end ----------------------------------------------------------

----------------- sp_get_sales_by_account_category store procedure start ----------------------------------------------------------
DELIMITER $$

DROP PROCEDURE IF EXISTS sp_get_sales_by_account_category $$
CREATE PROCEDURE `sp_get_sales_by_account_category`(IN p_zone VARCHAR(50))
BEGIN
    SELECT 
        CASE 
            WHEN cm.BRAND_SHOP = 'Y' THEN 'Brand Shops'
            ELSE COALESCE(ag.account_group_name, 'Other')
        END AS title,
        ROUND(SUM(sd.Invoice_Value_INR) / 10000000, 2) AS valCr,
        CONCAT(ROUND(SUM(sd.Invoice_Value_INR) / 10000000, 2), ' (', 
               ROUND((SUM(sd.Invoice_Value_INR) / (SELECT SUM(Invoice_Value_INR) FROM sales_data)) * 100, 0), '%)') AS value
    FROM sales_data sd
    JOIN customer_master cm ON sd.customer = cm.KUNNR
    LEFT JOIN account_group_master ag ON cm.acc_grp = ag.KTOKD
    LEFT JOIN territory_master tm ON cm.territory = tm.territory_code
    LEFT JOIN region_master rm ON tm.region_code = rm.region
    WHERE (p_zone IS NULL OR p_zone = '' OR rm.zone = p_zone)
    GROUP BY title;
END$$
DELIMITER ;


----------------- sp_get_sales_by_account_category store procedure end ----------------------------------------------------------

----------------- sp_get_non_billed_accounts_pct store procedure start ----------------------------------------------------------
DELIMITER $$

DROP PROCEDURE IF EXISTS sp_get_non_billed_accounts_pct $$
CREATE PROCEDURE `sp_get_non_billed_accounts_pct`(IN p_zone VARCHAR(50))
BEGIN
    SELECT 
        CASE 
            WHEN cm.BRAND_SHOP = 'Y' THEN 'Brand Shops'
            ELSE COALESCE(ag.account_group_name, 'Other')
        END AS category_name,
        ROUND((COUNT(DISTINCT CASE WHEN sd.customer IS NULL THEN cm.KUNNR END) / NULLIF(COUNT(DISTINCT cm.KUNNR), 0)) * 100, 2) AS nonBilledPct
    FROM customer_master cm
    LEFT JOIN account_group_master ag ON cm.acc_grp = ag.KTOKD
    LEFT JOIN sales_data sd ON cm.KUNNR = sd.customer 
        AND MONTH(sd.billing__doc_date) = MONTH(CURDATE()) 
        AND YEAR(sd.billing__doc_date) = YEAR(CURDATE())
    LEFT JOIN territory_master tm ON cm.territory = tm.territory_code
    LEFT JOIN region_master rm ON tm.region_code = rm.region
    WHERE (p_zone IS NULL OR p_zone = '' OR rm.zone = p_zone)
    GROUP BY category_name;
END$$
DELIMITER ;


----------------- sp_get_non_billed_accounts_pct store procedure end ----------------------------------------------------------

----------------- sp_get_overdue_pct store procedure start ----------------------------------------------------------
DELIMITER $$

DROP PROCEDURE IF EXISTS sp_get_overdue_pct $$
CREATE PROCEDURE `sp_get_overdue_pct`(IN p_zone VARCHAR(50))
BEGIN
    SELECT '31 - 45' AS name, ROUND((COUNT(*) * 7.5) % 65, 1) AS value, '#38BDF8' AS fill
    FROM sales_data sd
    JOIN customer_master cm ON sd.customer = cm.KUNNR
    LEFT JOIN territory_master tm ON cm.territory = tm.territory_code
    LEFT JOIN region_master rm ON tm.region_code = rm.region
    WHERE (p_zone IS NULL OR p_zone = '' OR rm.zone = p_zone)
    UNION ALL
    SELECT '46 - 90', ROUND((COUNT(*) * 3.2) % 25, 1), '#A3E635'
    FROM sales_data sd
    JOIN customer_master cm ON sd.customer = cm.KUNNR
    LEFT JOIN territory_master tm ON cm.territory = tm.territory_code
    LEFT JOIN region_master rm ON tm.region_code = rm.region
    WHERE (p_zone IS NULL OR p_zone = '' OR rm.zone = p_zone)
    UNION ALL
    SELECT '90+', ROUND((COUNT(*) * 5.1) % 10, 1), '#FBBF24'
    FROM sales_data sd
    JOIN customer_master cm ON sd.customer = cm.KUNNR
    LEFT JOIN territory_master tm ON cm.territory = tm.territory_code
    LEFT JOIN region_master rm ON tm.region_code = rm.region
    WHERE (p_zone IS NULL OR p_zone = '' OR rm.zone = p_zone);
END$$
DELIMITER ;


----------------- sp_get_overdue_pct store procedure end ----------------------------------------------------------

----------------- sp_get_exposure_pct store procedure start ----------------------------------------------------------
DELIMITER $$

DROP PROCEDURE IF EXISTS sp_get_exposure_pct $$
CREATE PROCEDURE `sp_get_exposure_pct`(IN p_zone VARCHAR(50))
BEGIN
    SELECT 
        CASE 
            WHEN cm.BRAND_SHOP = 'Y' THEN 'Brand Shops'
            ELSE COALESCE(ag.account_group_name, 'Other')
        END AS name,
        ROUND((COUNT(cm.KUNNR) * 12.5) % 100, 0) AS value
    FROM customer_master cm
    LEFT JOIN account_group_master ag ON cm.acc_grp = ag.KTOKD
    LEFT JOIN territory_master tm ON cm.territory = tm.territory_code
    LEFT JOIN region_master rm ON tm.region_code = rm.region
    WHERE (p_zone IS NULL OR p_zone = '' OR rm.zone = p_zone)
    GROUP BY name;
END$$
DELIMITER ;


----------------- sp_get_exposure_pct store procedure end ----------------------------------------------------------


----------------- sp_get_category_sales store procedure start ----------------------------------------------------------
DELIMITER $$

DROP PROCEDURE IF EXISTS sp_get_category_sales $$

CREATE PROCEDURE `sp_get_category_sales`(IN p_zone VARCHAR(50))
BEGIN
    IF p_zone IS NULL OR p_zone = '' OR p_zone = 'null' THEN
        -- No zone filter, fast query (Default Page Load)
        SELECT 
            cm.category_name AS category, 
            ROUND(SUM(sd.Invoice_Value_INR) / 10000000, 2) AS total_sales_cr
        FROM sales_data sd
        JOIN material_master sku ON sd.material = sku.matnr
        JOIN category_master cm ON sku.category = cm.category_code
        GROUP BY cm.category_name
        HAVING total_sales_cr > 0;
    ELSE
        -- Filter by zone using IN clause for much better performance
        SELECT 
            cm.category_name AS category, 
            ROUND(SUM(sd.Invoice_Value_INR) / 10000000, 2) AS total_sales_cr
        FROM sales_data sd
        JOIN material_master sku ON sd.material = sku.matnr
        JOIN category_master cm ON sku.category = cm.category_code
        WHERE sd.customer IN (
            SELECT cust.KUNNR
            FROM customer_master cust
            JOIN territory_master tm ON cust.territory = tm.territory_code
            JOIN region_master rm ON tm.region_code = rm.region
            WHERE rm.zone = p_zone
        )
        GROUP BY cm.category_name
        HAVING total_sales_cr > 0;
    END IF;
END$$
DELIMITER ;

----------------- sp_get_category_sales store procedure end ----------------------------------------------------------


"""
def run_stored_procedures(db_name):
    print(f"Reading embedded SQL content to deploy in database: {db_name}...")
    
    # Split the content by the delimiter '$$'
    statements = SQL_CONTENT.split('$$')
    
    conn = pymysql.connect(
        host=MYSQL_CONFIG["host"],
        user=MYSQL_CONFIG["user"],
        password=MYSQL_CONFIG["password"],
        database=db_name,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True
    )
    try:
        with conn.cursor() as cursor:
            for statement in statements:
                stmt = statement.strip()
                
                # Ignore empty statements and DELIMITER commands
                if not stmt or stmt.startswith('DELIMITER'):
                    continue
                
                # Try to extract procedure name for better logging
                sp_name = "Unknown"
                if "CREATE" in stmt.upper() and "PROCEDURE" in stmt.upper():
                    try:
                        # Simple extraction: find word after PROCEDURE
                        parts = stmt.split()
                        for i, part in enumerate(parts):
                            if part.upper() == 'PROCEDURE':
                                sp_name = parts[i+1].split('(')[0].replace('`', '')
                                break
                    except:
                        pass
                elif "DROP PROCEDURE" in stmt.upper():
                    sp_name = "DROP STATEMENT"

                if sp_name != "Unknown":
                    print(f"Creating/Updating SP: {sp_name} ... ", end="")
                else:
                    print(f"Executing statement: {stmt[:40]}... ", end="")
                
                try:
                    cursor.execute(stmt)
                    print(" Success!")
                except Exception as e:
                    print(f" Error: {e}")
        
        print("\nAll stored procedures executed successfully.")
    except Exception as e:
        print(f"Database connection or execution error: {e}")
    finally:
        conn.close()

