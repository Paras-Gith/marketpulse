import json
import re
from groq import Groq
from config import GROQ_API_KEY, MODEL_NAME, SYSTEM_PROMPT

client = Groq(api_key=GROQ_API_KEY)


def classify_headline(headline: str) -> dict:
    """Send a single headline to Groq (free tier) and return parsed JSON classification."""
    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            max_tokens=100,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": headline},
            ],
        )

        raw_text = response.choices[0].message.content.strip()
        raw_text = re.sub(r"^```json|```$", "", raw_text, flags=re.MULTILINE).strip()

        result = json.loads(raw_text)
        return {
            "headline": headline,
            "category": result.get("category", "Other"),
            "confidence": result.get("confidence", 0.0),
        }

    except json.JSONDecodeError:
        return {"headline": headline, "category": "Other", "confidence": 0.0, "error": "invalid_json"}
    except Exception as e:
        return {"headline": headline, "category": "Other", "confidence": 0.0, "error": str(e)}