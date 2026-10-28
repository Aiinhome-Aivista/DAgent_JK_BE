import requests
import json
import PyPDF2
from database.config import MISTRAL_API_KEY, MISTRAL_MODEL

class LLMService:
    def __init__(self):
        # Mistral Cloud Endpoint
        self.api_url = "https://api.mistral.ai/v1/chat/completions"
        self.api_key = MISTRAL_API_KEY
        self.model = MISTRAL_MODEL

    def extract_text_from_pdf(self, pdf_path):
        text = ""
        try:
            with open(pdf_path, 'rb') as file:
                reader = PyPDF2.PdfReader(file)
                for page in reader.pages:
                    text += page.extract_text()
        except Exception as e:
            print(f"Error extracting PDF: {e}")
        return text

    def get_topics_and_connections(self, books_content):
        """
        Takes a dict of {filename: content} and returns a unified mapping using Mistral Cloud.
        """
        # Format the data clearly for the LLM
        formatted_data = ""
        for filename, content in books_content.items():
            formatted_data += f"--- FILENAME: {filename} ---\nCONTENT SUMMARY: {content[:4000]}\n\n"

        prompt = f"""
        Analyze the following text from various books.
        {formatted_data}
        
        Tasks:
        1. For EACH book listed above, extract exactly 5-8 major topics.
        2. Use the EXACT filename provided (e.g., 'STRESS_RELIEF.pdf') as the 'name' field.
        3. Identify topics that are conceptually the same across different books and use the EXACT same string for them.
        
        Output ONLY a JSON object in this format:
        {{
          "books": [
            {{ "name": "FILENAME_HERE", "topics": ["Major Topic 1", "Major Topic 2", ...] }}
          ],
          "common_topics": ["Specific Shared Topic 1", ...]
        }}
        
        CRITICAL: Do not invent names. Use the filenames provided in the Headers above.
        """
        
        # Headers required for Mistral Cloud
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        # Payload formatted for Chat Completions API
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant that outputs strictly valid JSON."},
                {"role": "user", "content": prompt}
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.3
        }

        try:
            response = requests.post(self.api_url, headers=headers, json=payload)
            response_json = response.json()

            # Debugging: Print error if API fails
            if response.status_code != 200:
                print(f"Mistral API Error: {response.status_code} - {response.text}")
                return {"books": [], "common_topics": []}

            # Extract content from Mistral Cloud response structure
            if "choices" in response_json and len(response_json["choices"]) > 0:
                content_string = response_json["choices"][0]["message"]["content"]
                result = json.loads(content_string)
                return result
            else:
                print(f"Unexpected response structure: {response_json}")
                return {"books": [], "common_topics": []}

        except Exception as e:
            print(f"Error calling LLM for unified analysis: {e}")
            return {"books": [], "common_topics": []}