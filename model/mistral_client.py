import requests
from database.config import MISTRAL_API_KEY  

MISTRAL_API_URL = "http://localhost:11434/api/generate"  

def call_mistral(prompt: str) -> str:
    api_key = MISTRAL_API_KEY

    #  If API key exists → Use official Mistral Cloud API
    if api_key and api_key.strip():
        print("Using Mistral Cloud API key...")
        try:
            url = "https://api.mistral.ai/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": "mistral-large-latest", 
                "messages": [{"role": "user", "content": prompt}]
            }
            response = requests.post(url, headers=headers, json=payload)

            if response.status_code != 200:
                raise RuntimeError(f"Mistral Cloud API error: {response.status_code} {response.text}")

            resp = response.json()
            return resp["choices"][0]["message"]["content"].strip()

        except Exception as e:
            raise RuntimeError(f"Error calling Mistral Cloud API: {e}")

    # Else fallback to local Ollama
    print("using local mistral")
    try:
        response = requests.post(
            MISTRAL_API_URL,
            json={"model": "mistral", "prompt": prompt, "stream": False}
        )
        response.raise_for_status()
        return response.json()["response"].strip()
    except requests.RequestException as e:
        raise RuntimeError(f"Error calling Mistral API: {e}")