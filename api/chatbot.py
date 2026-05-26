from langchain_openai import ChatOpenAI
import json
import os
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# gpt-5-nano: cheapest, sufficient for JSON extraction tasks
DEFAULT_MODEL = "gpt-5-nano"

model_chatbots = {}


def get_chatbot(model_name=DEFAULT_MODEL):
    """Get or create a ChatOpenAI instance for the given model."""
    try:
        if model_name not in model_chatbots:
            model_chatbots[model_name] = ChatOpenAI(
                model=model_name, api_key=OPENAI_API_KEY
            )
        return model_chatbots[model_name]
    except Exception as e:
        print(f"Error initialising model {model_name}: {e}")
        if model_name != DEFAULT_MODEL:
            if DEFAULT_MODEL not in model_chatbots:
                model_chatbots[DEFAULT_MODEL] = ChatOpenAI(
                    model=DEFAULT_MODEL, api_key=OPENAI_API_KEY
                )
            return model_chatbots[DEFAULT_MODEL]
        raise


def clean_json_response(content):
    """Strip markdown code fences from LLM JSON output."""
    if "```" in content:
        think_content = content.split("```")[-1].strip()
        if think_content.startswith(("```json", "```")):
            content = think_content
        elif "{" in think_content and "}" in think_content:
            j_start = think_content.find("{")
            j_end = think_content.rfind("}") + 1
            if j_start >= 0 and j_end > j_start:
                content = think_content[j_start:j_end]

    if content.startswith("```") and content.endswith("```"):
        content = "\n".join(content.split("\n")[1:-1])
    elif content.startswith("```json") and content.endswith("```"):
        content = "\n".join(content.split("\n")[1:-1])

    return content.strip().replace("```json", "").replace("```", "").strip()


def is_recent_release(date_string, months_threshold=6):
    """True if release date is within the last N months."""
    if not date_string:
        return False
    try:
        release_date = datetime.strptime(date_string, "%Y-%m-%d")
        return (datetime.now() - release_date).days <= (months_threshold * 30)
    except Exception:
        return False


def is_upcoming_release(date_string):
    """True if release date is in the future."""
    if not date_string:
        return False
    try:
        return datetime.strptime(date_string, "%Y-%m-%d") > datetime.now()
    except Exception:
        return False


def extract_media_with_llm(bot_reply, model_name=DEFAULT_MODEL):
    """Extract movie/TV titles from a chatbot response using gpt-4.1-mini."""
    llm_prompt = (
        "You are an expert text analyser. Extract all movie, TV show, "
        "anime movie, and anime series titles from the chatbot response.\n\n"
        "Rules:\n"
        "- Extract title and release year (null if not mentioned)\n"
        "- Separate movies/anime movies from TV shows/anime series\n"
        "- Return ONLY valid JSON, no extra text\n\n"
        "Example output:\n"
        '{{"movies": [{{"title": "Inception", "year": 2010}}], '
        '"tv_shows": [{{"title": "Breaking Bad", "year": 2008}}]}}\n\n'
        f'Chatbot response:\n"{bot_reply}"'
    )
    try:
        chatbot = get_chatbot(model_name)
        response = chatbot.invoke(llm_prompt)
        cleaned = clean_json_response(response.content)
        data = json.loads(cleaned)
        return data.get("movies", []), data.get("tv_shows", [])
    except json.JSONDecodeError as e:
        print(f"Invalid JSON from LLM media extraction: {e}")
        return [], []
    except Exception as e:
        print(f"Error in LLM media extraction: {e}")
        return [], []


def is_safety_model_response(content, model_name):
    """True if this is a safety-model 'safe' response."""
    if model_name == "meta-llama/llama-guard-4-12b":
        return content.lower().strip() == "safe"
    return False
