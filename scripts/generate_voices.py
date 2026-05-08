"""Generiert MP3s mit Microsoft-Neural-Voice (klingt fast menschlich).
Free, kein API-Key nötig.

Du kannst die Stimme einfach austauschen:
- de-DE-ConradNeural   → männlich, ruhig, professionell ⭐
- de-DE-KatjaNeural    → weiblich, warm, professionell
- de-DE-FlorianMultilingualNeural → männlich, modern
- de-DE-BerndNeural    → männlich, tiefer
- de-DE-AmalaNeural    → weiblich, freundlich
- de-DE-KillianNeural  → männlich, jünger

Run: python3 scripts/generate_voices.py
"""
import asyncio
import edge_tts
import os
import sys

OUT = os.path.join(os.path.dirname(__file__), '..', 'static', 'voices')
os.makedirs(OUT, exist_ok=True)

# Wähle Stimme:
VOICE = 'de-DE-ConradNeural'  # männlich, ruhig — passt zum Najib-Founder-Vibe
# Pace: -10% = etwas langsamer, natürlicher
RATE = '-5%'

TEXTS = {
    # ======== ONBOARDING WIZARD ========
    'willkommen_1': "Schön dass du da bist. Willkommen bei NT Pro Academy. Hier ist dein Cockpit für alles was im Strukturvertrieb wichtig ist — Karriere, Provisionen, Coaching, dein Team. In den nächsten Minuten zeige ich dir wie's funktioniert. Lass uns starten.",

    'willkommen_2': "Bei uns gibt's sechs Karrierestufen. Du startest als Repräsentant. Bei tausend Einheiten wirst du Leitender Repräsentant. Dann geht's weiter über Hauptrepräsentant, Coordinator, Direktor — bis hoch zum Generalrepräsentanten. Jede Stufe bringt dir mehr Provision pro Einheit. Die Beförderung passiert automatisch sobald du die Kriterien erfüllst.",

    'willkommen_3': "Und jetzt zur wichtigsten Frage: warum bist du hier? Was ist deine Vision? Schreib in zwei oder drei Sätzen auf was dich antreibt. Das ist dein Anker für die Tage an denen's mal hart wird. Die App erinnert dich regelmäßig dran.",

    'willkommen_4': "Dein KI-Mentor begleitet dich. Er gibt dir tägliche Empfehlungen, hilft beim Coaching deiner Partner, schreibt Wochen-Briefings — und beantwortet deine Fragen rund um den Vertrieb. Du findest ihn jederzeit oben rechts unter dem Sprechblasen-Icon.",

    'willkommen_5': "Das war's mit dem Schnellstart. Jetzt zeige ich dir das Cockpit live. Klick auf Weiter, und ich führe dich durch die wichtigsten Bereiche. Los geht's.",

    # ======== APP-TOUR ========
    'tour_1': "Das hier ist dein Dashboard. Oben siehst du deinen aktuellen Stand: deine Einheiten, deine Karrierestufe, dein Team. Alle Zahlen aktualisieren sich live, sobald jemand etwas einträgt.",

    'tour_2': "Hier links findest du das Hauptmenü. Leads sind deine Anwärter, Verträge deine Abschlüsse, Team deine Struktur. Im Coaching-Bereich hilft dir der KI-Mentor mit Tipps und Diagnosen — und in den Stats siehst du wer in dieser Woche vorne liegt.",

    'tour_3': "Wichtig: Einheiten zählen erst wenn die Recherche freigegeben wurde. Das heißt: ein Vertrag ist nicht durch sobald er unterschrieben ist — er muss komplett geprüft sein. Das schützt dich und deine Partner vor falschen Versprechen.",

    'tour_4': "Oben rechts siehst du den Eingabeschluss und das nächste Grundseminar. Eingabeschluss ist der dritte Werktag des Monats — bis dahin müssen alle Verträge eingereicht sein. Das Grundseminar findet am zweiten Samstag danach statt. Plan deine Termine entsprechend.",

    'tour_5': "Trophäen sammeln macht Spaß. Es gibt physische Geschenke: einen Ferrari-Pin bei neunundneunzig Einheiten, einen Montblanc-Stift bei der HREP-Stufe, eine Breitling-Uhr beim Coordinator. Die Übersicht findest du unter Trophäen.",

    'tour_6': "Das war's mit der Tour. Du kannst sie jederzeit über das Fragezeichen oben nochmal starten. Jetzt: leg los. Trag deinen ersten Lead ein — und wir sehen uns auf der Bestenliste.",
}


async def gen_one(name, text):
    path = os.path.join(OUT, f'{name}.mp3')
    com = edge_tts.Communicate(text, VOICE, rate=RATE)
    await com.save(path)
    size = os.path.getsize(path) / 1024
    print(f'✓ {name}.mp3 ({size:.0f} KB) — "{text[:60]}…"')


async def main():
    print(f'\n🎙  Stimme: {VOICE} ({RATE})')
    print(f'📁  Output: {OUT}\n')
    for name, text in TEXTS.items():
        await gen_one(name, text)
    print(f'\n✅ Fertig. {len(TEXTS)} MP3s generiert.')


if __name__ == '__main__':
    asyncio.run(main())
