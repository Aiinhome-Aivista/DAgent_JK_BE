"""
Helper utility for parsing dynamic period parameters (Single Month, Multi-Month, Custom Date Range)
and building uniform SQL date conditions, meta headers, and cache keys across all 5 sales summary controllers:
  1. Domestic Sales Value Achievement (Sheet 1)
  2. Sales Number's (Sheet 2)
  3. Sales Report by Values (Sheet 3)
  4. Sales Summary in No's & Values (Sheet 4)
  5. Zone-Wise Sales Achievement
"""

import calendar
from datetime import datetime, date

def parse_period_args(
    args_or_json,
    default_year=None,
    default_month=None,
    default_day=None,
    detected_year=None,
    detected_month=None,
    detected_day=None,
    master_months=None,
    **kwargs
):
    """
    Parses request arguments / JSON payload for:
      - month (single month 1..12 or abbreviation 'NOV')
      - months (multiple months e.g. '10,11' or [10, 11])
      - start_date (YYYY-MM-DD)
      - end_date (YYYY-MM-DD)
      - year_month (YYYYMM e.g. '202611')
    Returns a unified period dictionary.
    """
    now = datetime.now()
    cur_yr = int(default_year or detected_year or now.year)
    cur_mo = int(default_month or detected_month or now.month)
    cur_d = int(default_day or detected_day or now.day)

    data = args_or_json or {}
    if not isinstance(data, dict) and hasattr(data, "to_dict"):
        data = data.to_dict()

    raw_start = str(data.get("start_date") or "").strip()
    raw_end = str(data.get("end_date") or "").strip()
    raw_months = data.get("months")
    raw_month = data.get("month") or data.get("m")
    raw_ym = str(data.get("year_month") or data.get("ym") or "").strip()
    raw_year = data.get("year")

    if raw_year and str(raw_year).strip().isdigit():
        cur_yr = int(str(raw_year).strip())

    # Mode 1: Custom Date Range (start_date and end_date provided)
    if raw_start and raw_end:
        try:
            s_dt = datetime.strptime(raw_start, "%Y-%m-%d").date()
            e_dt = datetime.strptime(raw_end, "%Y-%m-%d").date()
            if s_dt > e_dt:
                s_dt, e_dt = e_dt, s_dt
            
            curr_from = s_dt.strftime("%Y-%m-%d")
            curr_to = e_dt.strftime("%Y-%m-%d")
            prev_from = (s_dt.replace(year=s_dt.year - 1)).strftime("%Y-%m-%d")
            prev_to = (e_dt.replace(year=e_dt.year - 1)).strftime("%Y-%m-%d")
            day_date = curr_to

            # Collect all distinct year-months spanned by the date range for Target aggregation
            t_ymonths = []
            tmp_yr, tmp_mo = s_dt.year, s_dt.month
            while (tmp_yr < e_dt.year) or (tmp_yr == e_dt.year and tmp_mo <= e_dt.month):
                t_ymonths.append(f"{tmp_yr}{tmp_mo:02d}")
                tmp_mo += 1
                if tmp_mo > 12:
                    tmp_mo = 1
                    tmp_yr += 1

            cache_key = f"r_{s_dt.strftime('%Y%m%d')}_{e_dt.strftime('%Y%m%d')}"

            s_str = s_dt.strftime("%d %b")
            e_str = e_dt.strftime("%d %b %Y")
            e_str_prev = f"{e_dt.strftime('%d %b')} {s_dt.year - 1}"

            return {
                "mode": "date_range",
                "curr_from": curr_from,
                "curr_to": curr_to,
                "prev_from": prev_from,
                "prev_to": prev_to,
                "day_date": day_date,
                "target_ymonths": t_ymonths,
                "cache_key": cache_key,
                "selected_month": s_dt.month,
                "meta": {
                    "month_abbr": f"{s_dt.strftime('%b %d')} - {e_dt.strftime('%b %d')}",
                    "prev_year_label": f"Actual ({s_str} - {e_str_prev})",
                    "curr_year_label": f"Actual Sale ({s_str} - {e_str})",
                    "prev_year_tooltip": f"Previous Year Range: {prev_from} to {prev_to}",
                    "curr_year_tooltip": f"Selected Date Range: {curr_from} to {curr_to}",
                    "day_label": f"Sale for day {e_dt.day}",
                    "filter_mode": "date_range",
                    "start_date": curr_from,
                    "end_date": curr_to
                }
            }
        except Exception as e:
            pass

    # Mode 2: Multi-Month Selection
    parsed_months = []
    if raw_months:
        if isinstance(raw_months, (list, tuple)):
            for item in raw_months:
                if str(item).strip().isdigit():
                    m_val = int(str(item).strip())
                    if 1 <= m_val <= 12 and m_val not in parsed_months:
                        parsed_months.append(m_val)
        elif isinstance(raw_months, str):
            for part in raw_months.split(","):
                part_clean = part.strip()
                if part_clean.isdigit():
                    m_val = int(part_clean)
                    if 1 <= m_val <= 12 and m_val not in parsed_months:
                        parsed_months.append(m_val)
                else:
                    found = next((m for m in range(1, 13) if calendar.month_abbr[m].upper() == part_clean.upper()), None)
                    if found and found not in parsed_months:
                        parsed_months.append(found)

    # Mode 3: Single Month (from month or year_month)
    if not parsed_months:
        single_m = None
        if raw_month:
            clean_m = str(raw_month).strip().upper()
            if clean_m.isdigit():
                single_m = int(clean_m)
            else:
                found = next((m for m in range(1, 13) if calendar.month_abbr[m].upper() == clean_m), None)
                if found:
                    single_m = found
        elif raw_ym and len(raw_ym) >= 6 and raw_ym[:6].isdigit():
            cur_yr = int(raw_ym[:4])
            single_m = int(raw_ym[4:6])

        if single_m and 1 <= single_m <= 12:
            parsed_months = [single_m]
        else:
            parsed_months = [cur_mo]

    parsed_months.sort()

    # Single Month Mode
    if len(parsed_months) == 1:
        m_num = parsed_months[0]
        last_d = calendar.monthrange(cur_yr, m_num)[1]
        active_d = cur_d if m_num == cur_mo else last_d
        active_d = min(active_d, last_d)

        curr_from = f"{cur_yr}-{m_num:02d}-01"
        curr_to = f"{cur_yr}-{m_num:02d}-{active_d:02d}"
        
        last_d_prev = calendar.monthrange(cur_yr - 1, m_num)[1]
        prev_from = f"{cur_yr - 1}-{m_num:02d}-01"
        prev_to = f"{cur_yr - 1}-{m_num:02d}-{last_d_prev:02d}"
        day_date = f"{cur_yr}-{m_num:02d}-{active_d:02d}"

        ym_str = f"{cur_yr}{m_num:02d}"
        m_abbr = calendar.month_abbr[m_num]
        prev_yr_2d = str(cur_yr - 1)[-2:]
        curr_yr_2d = str(cur_yr)[-2:]

        return {
            "mode": "single_month",
            "curr_from": curr_from,
            "curr_to": curr_to,
            "prev_from": prev_from,
            "prev_to": prev_to,
            "day_date": day_date,
            "target_ymonths": [ym_str],
            "cache_key": ym_str,
            "selected_month": m_num,
            "meta": {
                "month_abbr": m_abbr,
                "prev_year_label": f"{m_abbr}-{prev_yr_2d} Actual",
                "curr_year_label": f"Actual Sale for {m_abbr}-{curr_yr_2d}",
                "prev_year_tooltip": f"{m_abbr} {cur_yr - 1} Actual Sales",
                "curr_year_tooltip": f"Actual Sales for {m_abbr} {cur_yr}",
                "day_label": f"Sale for day {active_d}",
                "filter_mode": "single_month",
                "months": [m_num]
            }
        }

    # Multiple Months Mode
    first_m = parsed_months[0]
    last_m = parsed_months[-1]
    last_d = calendar.monthrange(cur_yr, last_m)[1]
    active_d = cur_d if last_m == cur_mo else last_d
    active_d = min(active_d, last_d)

    curr_from = f"{cur_yr}-{first_m:02d}-01"
    curr_to = f"{cur_yr}-{last_m:02d}-{active_d:02d}"

    last_d_prev = calendar.monthrange(cur_yr - 1, last_m)[1]
    prev_from = f"{cur_yr - 1}-{first_m:02d}-01"
    prev_to = f"{cur_yr - 1}-{last_m:02d}-{last_d_prev:02d}"
    day_date = f"{cur_yr}-{last_m:02d}-{active_d:02d}"

    t_ymonths = [f"{cur_yr}{m:02d}" for m in parsed_months]
    cache_key = f"m_{cur_yr}_{'_'.join(str(m) for m in parsed_months)}"

    prev_yr_2d = str(cur_yr - 1)[-2:]
    curr_yr_2d = str(cur_yr)[-2:]
    full_month_names = ", ".join(calendar.month_abbr[m] for m in parsed_months)
    prev_tooltip = f"Selected Months ({cur_yr - 1}): {full_month_names}"
    curr_tooltip = f"Selected Months ({cur_yr}): {full_month_names}"

    if len(parsed_months) <= 2:
        m_abbrs = ", ".join(calendar.month_abbr[m] for m in parsed_months)
        prev_label = f"{m_abbrs} '{prev_yr_2d} Act"
        curr_label = f"Actual Sale for {m_abbrs} '{curr_yr_2d}"
    else:
        first_two = ", ".join(calendar.month_abbr[m] for m in parsed_months[:2])
        remainder = len(parsed_months) - 2
        prev_label = f"{first_two} +{remainder} '{prev_yr_2d} Act"
        curr_label = f"Actual Sale for {first_two} +{remainder} '{curr_yr_2d}"

    return {
        "mode": "multi_month",
        "curr_from": curr_from,
        "curr_to": curr_to,
        "prev_from": prev_from,
        "prev_to": prev_to,
        "day_date": day_date,
        "target_ymonths": t_ymonths,
        "cache_key": cache_key,
        "selected_month": parsed_months[0],
        "meta": {
            "month_abbr": full_month_names,
            "prev_year_label": prev_label,
            "curr_year_label": curr_label,
            "prev_year_tooltip": prev_tooltip,
            "curr_year_tooltip": curr_tooltip,
            "day_label": f"Sale for day {active_d}",
            "filter_mode": "multi_month",
            "months": parsed_months
        }
    }

