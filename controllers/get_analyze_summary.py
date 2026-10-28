import json
import traceback
from flask import request, jsonify
from sqlalchemy import text
from database.config import engine
from flask_cors import CORS

def get_analyze_summary_controller():
    try:
        # Expect session_name in request body
        data = request.get_json(silent=True) or {}
        session_name = data.get("session_name")

        if not session_name:
            return jsonify({
                "status": "failed",
                "message": "Missing required parameter: session_name"
            }), 400

        # Fetch analyze record
        with engine.begin() as conn:
            result = conn.execute(
                text("SELECT response, processing_status FROM `analyze` WHERE session_name = :sn"),
                {"sn": session_name},
            ).fetchone()

        if not result:
            return jsonify({
                "status": "failed",
                "message": f"No analysis found for session_name '{session_name}'"
            }), 404

        response_data, processing_status = result
        total_files = 0
        total_rows = 0
        total_columns = 0
        avg_quality_score = 0
        file_quality_list = []

        try:
            response_json = json.loads(response_data)
            files_data = response_json.get("files", {})

            total_files = len(files_data)
            row_counts = []
            col_counts = []

            # For each file, compute stats
            for fname, fdata in files_data.items():
                metadata = fdata.get("metadata", {})
                word_count = fdata.get("word_count", 0)

                row_counts.append(word_count)
                col_counts.append(len(metadata.keys()))

                # --- Quality Score Logic (0–70 out of 100%) ---
                # Ratio of metadata richness vs. file size
                if word_count > 0:
                    ratio = len(metadata.keys()) / word_count
                    # Scale to 0–70 out of 100%
                    quality_score = round(min(ratio * 100, 70), 2)
                else:
                    # No words → lowest possible quality
                    quality_score = 0

                file_quality_list.append({
                    "file_name": fname,
                    "quality_score": quality_score
                })

            # Total stats
            total_rows = sum(row_counts)
            total_columns = sum(col_counts)

            # Average Quality Score (0–70)
            if file_quality_list:
                avg_quality_score = round(
                    min(sum(f["quality_score"] for f in file_quality_list) / len(file_quality_list), 70),
                    2
                )

        except Exception as e:
            print("Error parsing response JSON:", e)

        # Final response
        return jsonify({
            "status": "success",
            "message": "Analysis summary retrieved successfully.",
            "processing_status": processing_status,
            "data": {
                "session_name": session_name,
                "total_files_uploaded": total_files,
                "total_rows": total_rows,
                "total_columns": total_columns,
                "average_quality_score": avg_quality_score,  # 0–70 out of 100
                "file_wise_quality": file_quality_list
            }
        }), 200

    except Exception as e:
        print("Error in get_analyze_summary:", traceback.format_exc())
        return jsonify({
            "status": "failed",
            "message": "Internal server error.",
            "error": str(e)
        }), 500
