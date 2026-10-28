import json
import mysql.connector
from database.config import MYSQL_CONFIG


def get_db_connection():
    """
    Creates and returns a MySQL database connection
    """
    try:
        conn = mysql.connector.connect(**MYSQL_CONFIG)
        return conn
    except mysql.connector.Error as err:
        raise Exception(f"Database connection error: {err}")


def fetch_user_answers(user_id):
    """
    Fetches user responses from DB and converts them into
    the format expected by the AI:

    {
        question_id: "answer"
        OR
        question_id: ["answer1", "answer2"]
    }
    """

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    query = """
        SELECT
            ur.question_id,
            ur.answer_value
        FROM user_responses ur
        INNER JOIN users u ON u.user_id = ur.user_id
        INNER JOIN questions q ON q.question_id = ur.question_id
        WHERE u.user_id = %s
        ORDER BY ur.question_id
    """

    cursor.execute(query, (user_id,))
    rows = cursor.fetchall()

    cursor.close()
    conn.close()

    user_answers = {}

    for row in rows:
        question_id = row["question_id"]
        answer_value = row["answer_value"]

        if answer_value and "," in answer_value:
            user_answers[question_id] = [
                ans.strip() for ans in answer_value.split(",")
            ]
        else:
            user_answers[question_id] = answer_value.strip(
            ) if answer_value else ""

    return user_answers


def save_ai_profile(user_id, ai_profile):
    conn = mysql.connector.connect(**MYSQL_CONFIG)
    cursor = conn.cursor()

    query = """
        INSERT INTO wellbeing_ai_results (user_id, ai_profile)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE
        ai_profile = VALUES(ai_profile),
        updated_at = CURRENT_TIMESTAMP
    """

    cursor.execute(query, (user_id, json.dumps(ai_profile)))
    conn.commit()

    cursor.close()
    conn.close()


def fetch_ai_profile(user_id):
    conn = mysql.connector.connect(**MYSQL_CONFIG)
    cursor = conn.cursor(dictionary=True)

    query = """
        SELECT ai_profile
        FROM wellbeing_ai_results
        WHERE user_id = %s
        ORDER BY updated_at DESC
        LIMIT 1
    """

    cursor.execute(query, (user_id,))
    result = cursor.fetchone()

    cursor.close()
    conn.close()

    if result:
        return json.loads(result["ai_profile"])

    return None


def save_recovery_plan(user_id, recovery_plan):
    conn = mysql.connector.connect(**MYSQL_CONFIG)
    cursor = conn.cursor()

    query = """
        INSERT INTO wellbeing_recovery_plans (user_id, recovery_plan)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE
        recovery_plan = VALUES(recovery_plan),
        updated_at = CURRENT_TIMESTAMP
    """

    cursor.execute(query, (user_id, json.dumps(recovery_plan)))
    conn.commit()

    cursor.close()
    conn.close()
