"""
Ollama'da çalışan Gemma 4 modeline transkripti gönderip özet alır.
Hem "canlı" (kısa, akan) hem "final" (webinar bitince, düzenli) formatı destekler.
"""

import requests

import config

LIVE_PROMPT = """Aşağıda bir webinarın o ana kadarki transkripti var. Türkçe olarak özetle.

Kurallar:
- Madde madde en önemli noktaları yaz.
- Varsa "Aksiyon Maddeleri / Kararlar" başlığı altında somut kararları veya yapılacakları ayrıca listele.
- Sadece transkriptte gerçekten geçenleri yaz, tahmin veya uydurma yapma.
- Kısa ve öz ol, bu senin ilk taslağın değil, canlı takip eden bir özet.

TRANSKRİPT:
{transcript}

ÖZET:"""

FINAL_PROMPT = """Aşağıda tamamlanmış bir webinarın tüm transkripti var. Kapsamlı ve düzenli bir final özeti hazırla.

Şu formatı kullan:

## Genel Özet
(webinarın ana konusu ve akışı, 3-5 cümle)

## Ana Noktalar
(madde madde en önemli içerik noktaları)

## Aksiyon Maddeleri / Kararlar
(varsa somut kararlar, yapılacaklar, deadline'lar; yoksa "Belirtilmedi" yaz)

Sadece transkriptte gerçekten geçenleri yaz, tahmin veya uydurma yapma.

TRANSKRİPT:
{transcript}

FİNAL ÖZET:"""


class Summarizer:
    def __init__(self):
        self._check_ollama()

    def _check_ollama(self):
        try:
            requests.get(f"{config.OLLAMA_URL}/api/tags", timeout=3)
        except requests.exceptions.ConnectionError:
            raise RuntimeError(
                "Ollama'ya bağlanılamadı. Ollama'nın kurulu ve çalışır durumda "
                f"olduğundan emin ol (varsayılan adres: {config.OLLAMA_URL}). "
                f"Model henüz indirilmediyse: 'ollama pull {config.OLLAMA_MODEL}'"
            )

    def summarize(self, transcript: str, is_final: bool = False) -> str:
        if not transcript.strip():
            return "Henüz yeterli transkript yok."

        prompt_template = FINAL_PROMPT if is_final else LIVE_PROMPT
        prompt = prompt_template.format(transcript=transcript)

        response = requests.post(
            f"{config.OLLAMA_URL}/api/generate",
            json={
                "model": config.OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {
                    # Ollama'nın 4096'lık varsayılan bağlamını ezmek şart, aksi halde
                    # uzun transkriptler sessizce kesilir (bkz. config.py'deki not).
                    "num_ctx": config.OLLAMA_NUM_CTX,
                    "temperature": 0.3,  # özetlemede tutarlılık, uydurma riskini azaltmak için düşük
                    "num_gpu": 0 if config.OLLAMA_FORCE_CPU else -1,
                },
            },
            timeout=600,  # uzun transkript + CPU üretimi zaman alabilir
        )
        response.raise_for_status()
        return response.json().get("response", "").strip()
