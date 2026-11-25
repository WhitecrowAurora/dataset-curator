"""动漫描述的发色提取和独立 VLM 判断；文字不转换成概率。"""
from __future__ import annotations

import base64
import io
import json
import re

import httpx
from PIL import Image

from ..netutil import trust_env_for
from .models import qwen_predict

TORII_SYSTEM = "You are image captioning expert. Describe user's picture according to requested format and instructions."
# Minthy/ToriiGate-0.5 scripts/prompts.py，固定 revision 7d6b237af26e69f6ee9b381fe7beb3340cecabae，MIT。
_TORII_JSON_EXAMPLE = '''{
  "General": "Briefly describe the overall composition and setting.",
  "character_1": "Describe the first visible human or humanlike character.",
  "image_effects": "Describe distinct visual effects, or None.",
  "texts": "Quote a short prominent legible phrase, briefly note dense text, or None.",
  "watermarks": "Briefly note a visible watermark without identifying its author, or None."
}'''

TORII_PROMPT = f'''# Captioning format:

Return exactly one valid JSON object and nothing else. Use double-quoted keys and string values; do not add Markdown fences, comments, ellipses, or trailing commas. Use this shape:
{_TORII_JSON_EXAMPLE}
Add one character_N field for each clearly visible human or humanlike character; omit character fields when none are present. Do not put a guessed name in a key. Keep descriptions concise and literal. Use "None" for absent image_effects, texts, or watermarks.

# Characters on picture:
Do not guess names for characters, artists, series or studios. Do not infer authorship from a signature or watermark. The annotation system adds any trusted artist metadata separately; do not write artist attribution or an artist-style prefix yourself.

# Visible text:
Focus on visible subjects and setting. For dense dialogue, many speech bubbles, repeated labels or tiny background print, briefly note their presence only when visually important; do not transcribe or retell their contents, count every bubble or list each text region.
Quote only short, prominent, clearly legible text that helps explain the image, keeping its original script and punctuation; never translate or romanise it. If text itself is the main subject, quote at most one short prominent phrase. Omit illegible text without guessing characters or adding unreadable placeholders. Put text details only in texts and watermark details only in watermarks; do not repeat them in General or character fields. A visible date or year does not establish when the artwork was made.

# Subject and hair boundary:
Call a subject a person only when it is a real person or a drawn humanlike character.
Describe animals, fur, manes, plush toys, dolls, mannequins, statues, sculptures and ordinary
objects as those kinds of subjects. Do not mistake animal fur or a mane, a doll's wig, or carved
hair on a statue for human scalp hair.
An upright, clothed or animal-headed creature is still an animal unless a separate human or clearly
humanlike drawn person is visible. Do not treat ears, horns, muzzles, feathers, scales or fur as
human scalp hair.
When a person is present, use girl, boy, woman, man or person in the first sentence of that
person's description. Do not turn a person's clothing, accessories, facial hair, lighting or
the background into hair.
'''
_COLORS = r"silver|white|grey|gray|blond(?:e)?|black|brown|blue|aqua|cyan|green|orange|pink|purple|red"
_HAIR = re.compile(r"\b((?:" + _COLORS + r")(?:[\s/-]+(?:and\s+)?(?:" + _COLORS + r"))*)\b(?:[\s,-]+(?:colored|coloured|wavy|straight|long|short|curly|flowing|spiky|tousled))*[\s-]+hair\b", re.I)
_HAIR_AFTER = re.compile(r"\bhair\s+(?:(?:is|in)\s+)?(?:deep\s+|light\s+|dark\s+)?(" + _COLORS + r")\b", re.I)
_PERSON_HAIR = re.compile(r"\b(" + _COLORS + r")\s+(?:girl|boy|woman|man)\s+with\b[^.!?]{0,50}\bhair\b", re.I)
_ALIASES = {"gray": "grey", "blond": "blonde", "cyan": "aqua"}
_PERSON = re.compile(r"\b(?:girls?|boys?|women|woman|men|man|person|people|humans?|humanoids?)\b", re.I)
_NONHUMAN = re.compile(r"\b(?:statue|sculpture|sculpted|carving|carved|bust|effigy|wax\s+figure|figurine|plush|mannequin|doll|quadrupedal|animal|cat|dog|bird|giraffe|parrot|macaw|swan|goat|beetle|crab|fish|coral|vehicle|boat|pool|furniture|table|bed|nightstands?|fan|fireplace|chairs?|room|quilt|box|jar|jug|objects?|leaf|tablet|columns?|kitchen|floor)\b", re.I)
_MULTIPLE_PEOPLE = re.compile(
    r"\b(?:two|three|four|five|six|seven|eight|nine|ten|several|multiple|many|both)\s+"
    r"(?:young\s+|adult\s+|anime\s+|female\s+|male\s+)?"
    r"(?:girls?|boys?|women|men|people|persons|characters?)\b",
    re.I,
)


def _text_fields(value, path=()):
    """Flatten Torii's occasional nested character objects without treating metadata as a person."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _text_fields(child, (*path, str(key)))


def _nonhuman_subject(key, subject):
    if _NONHUMAN.search(key):
        return True
    head = re.sub(
        r"^\s*(?:(?:the|a|an)\s+)?(?:image|picture|photo|photograph|scene)\s+"
        r"(?:shows?|features?|depicts?|contains?)\s+",
        "",
        subject,
        flags=re.I,
    )
    head = re.sub(
        r"^\s*(?:(?:the|a|an)\s+)?(?:close-up|closeup|portrait|illustration|drawing|painting|"
        r"photo|photograph|image|depiction|view)\s+of\s+",
        "",
        head,
        flags=re.I,
    )
    head = re.split(
        r"\b(?:with|wearing|wears|dressed|as|holding|carrying|in|on|near|beside|while|whose|is|has|and)\b",
        head,
        maxsplit=1,
        flags=re.I,
    )[0]
    return bool(_NONHUMAN.search(head))


def parse_torii(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("Torii 没有返回描述对象")
    characters = []
    for path, value in _text_fields(raw):
        key = path[-1] if path else "description"
        if re.match(r"^(?:general|background|image.effect|texts?|watermarks?|atmosphere|main content|composition|setting|style|lighting)\b", key, re.I):
            continue
        matches = _HAIR.findall(value) + _HAIR_AFTER.findall(value) + _PERSON_HAIR.findall(value)
        colors = list(dict.fromkeys(_ALIASES.get(c.lower(), c.lower()) for phrase in matches
                                   for c in re.findall(r"\b(?:" + _COLORS + r")\b", phrase, re.I)))
        subject = value.split(".", 1)[0]
        nonhuman = _nonhuman_subject(key, subject)
        person = bool(_PERSON.search(subject)) and not nonhuman
        characters.append({"name": ".".join(path) or key, "description": value,
                           "colors": colors, "nonhuman": nonhuman, "person": person})
    people = [c for c in characters if c["person"]]
    colors = list(dict.fromkeys(c for p in people for c in p["colors"]))
    text = " ".join(str(v) for v in raw.values())
    monochrome = bool(re.search(r"\b(?:monochrome|grayscale|greyscale|graphite|pencil sketch|black.and.white\s+(?:image|photo(?:graph)?|drawing|portrait|sketch|art))\b", text, re.I))
    hair_details = " ".join(sentence for p in people for sentence in re.split(r"[.!?]", p["description"])
                            if re.search(r"\bhair\b", sentence, re.I))
    uncertain = monochrome or bool(re.search(r"\b(?:streaks?|highlights?|gradient|two.toned)\b", hair_details, re.I))
    no_hair = not people and bool(characters) and all(c["nonhuman"] for c in characters)
    subject = "person" if people else "nonhuman" if no_hair else "uncertain"
    multiple_people = len(people) > 1 or any(_MULTIPLE_PEOPLE.search(p["description"]) for p in people)
    return {"subject": subject, "color": colors[0] if len(colors) == 1 and not uncertain else "",
            "candidate_colors": colors, "multiple_people": multiple_people,
            "multi_color": len(colors) > 1 or uncertain, "visible_hair": False if no_hair else True if colors else None,
            "reason": "灰度或发色细节有歧义" if uncertain else "按角色描述提取发色",
            "characters": characters, "caption": raw, "source": "independent_image"}


def torii_predict(image, url, model):
    budget = 1_000_000
    if image.width * image.height > budget:
        scale = (budget / (image.width * image.height)) ** .5
        image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.Resampling.LANCZOS)
    data = io.BytesIO()
    image.save(data, format="PNG")
    encoded = base64.b64encode(data.getvalue()).decode("ascii")
    with httpx.Client(timeout=120, trust_env=trust_env_for(url)) as client:
        response = client.post(url.rstrip("/") + "/v1/chat/completions", json={
            "model": model, "temperature": .5, "seed": 20261119, "max_tokens": 2048,
            "cache_prompt": False, "stop": ["<|im_end|>", "<|endoftext|>"],
            "messages": [{"role": "system", "content": [{"type": "text", "text": TORII_SYSTEM}]},
                         {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded}},
                                                       {"type": "text", "text": TORII_PROMPT}]}]})
        response.raise_for_status()
    choice = response.json()["choices"][0]
    if choice.get("finish_reason") == "length":
        raise ValueError("Torii 描述被截断")
    text = choice["message"]["content"].strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return parse_torii(json.loads(text))


def predict(image, kind, url, model):
    result = torii_predict(image, url, model) if kind == "torii" else qwen_predict(image, url, model)
    return {"model": "toriigate" if kind == "torii" else "qwen3vl", **result}
