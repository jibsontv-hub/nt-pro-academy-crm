"""Generiert MP3s mit Microsoft-Neural-Voice — DE / EN / FR.
Free, kein API-Key nötig.

Output:
  static/voices/de/*.mp3
  static/voices/en/*.mp3
  static/voices/fr/*.mp3

Run: python3 scripts/generate_voices.py
Run: python3 scripts/generate_voices.py de    (nur Deutsch)
"""
import asyncio
import edge_tts
import os
import sys

OUT = os.path.join(os.path.dirname(__file__), '..', 'static', 'voices')

# Stimmen — alle drei klingen ähnlich (männlich, professionell, ruhig)
VOICES = {
    'de': ('de-DE-ConradNeural', '-5%'),
    'en': ('en-US-GuyNeural', '-5%'),
    'fr': ('fr-FR-HenriNeural', '-5%'),
}

TEXTS = {
    # ============= DEUTSCH =============
    'de': {
        'willkommen_1': "Schön dass du da bist. Willkommen bei NT Pro Academy. Hier ist dein Cockpit für alles was im Strukturvertrieb wichtig ist — Karriere, Provisionen, Coaching, dein Team. In den nächsten Minuten zeige ich dir wie's funktioniert. Lass uns starten.",
        'willkommen_2': "Bei uns gibt's sechs Karrierestufen. Du startest als Repräsentant. Bei tausend Einheiten wirst du Leitender Repräsentant. Dann geht's weiter über Hauptrepräsentant, Coordinator, Direktor — bis hoch zum Generalrepräsentanten. Jede Stufe bringt dir mehr Provision pro Einheit. Die Beförderung passiert automatisch sobald du die Kriterien erfüllst.",
        'willkommen_3': "Und jetzt zur wichtigsten Frage: warum bist du hier? Was ist deine Vision? Schreib in zwei oder drei Sätzen auf was dich antreibt. Das ist dein Anker für die Tage an denen's mal hart wird. Die App erinnert dich regelmäßig dran.",
        'willkommen_4': "Dein KI-Mentor begleitet dich. Er gibt dir tägliche Empfehlungen, hilft beim Coaching deiner Partner, schreibt Wochen-Briefings — und beantwortet deine Fragen rund um den Vertrieb. Du findest ihn jederzeit oben rechts unter dem Sprechblasen-Icon.",
        'willkommen_5': "Das war's mit dem Schnellstart. Jetzt zeige ich dir das Cockpit live. Klick auf Weiter, und ich führe dich durch die wichtigsten Bereiche. Los geht's.",
        'tour_1': "Das hier ist dein Dashboard. Oben siehst du deinen aktuellen Stand: deine Einheiten, deine Karrierestufe, dein Team. Alle Zahlen aktualisieren sich live, sobald jemand etwas einträgt.",
        'tour_2': "Hier links findest du das Hauptmenü. Leads sind deine Anwärter, Verträge deine Abschlüsse, Team deine Struktur. Im Coaching-Bereich hilft dir der KI-Mentor mit Tipps und Diagnosen — und in den Stats siehst du wer in dieser Woche vorne liegt.",
        'tour_3': "Wichtig: Einheiten zählen erst wenn die Recherche freigegeben wurde. Das heißt: ein Vertrag ist nicht durch sobald er unterschrieben ist — er muss komplett geprüft sein. Das schützt dich und deine Partner vor falschen Versprechen.",
        'tour_4': "Oben rechts siehst du den Eingabeschluss und das nächste Grundseminar. Eingabeschluss ist der dritte Werktag des Monats — bis dahin müssen alle Verträge eingereicht sein. Das Grundseminar findet am zweiten Samstag danach statt. Plan deine Termine entsprechend.",
        'tour_5': "Trophäen sammeln macht Spaß. Es gibt physische Geschenke: einen Ferrari-Pin bei neunundneunzig Einheiten, einen Montblanc-Stift bei der HREP-Stufe, eine Breitling-Uhr beim Coordinator. Die Übersicht findest du unter Trophäen.",
        'tour_6': "Das war's mit der Tour. Du kannst sie jederzeit über das Fragezeichen oben nochmal starten. Jetzt: leg los. Trag deinen ersten Lead ein — und wir sehen uns auf der Bestenliste.",
    },
    # ============= ENGLISH =============
    'en': {
        'willkommen_1': "Glad you're here. Welcome to NT Pro Academy. This is your cockpit for everything that matters in structured sales — career, commissions, coaching, your team. In the next few minutes, I'll show you how it works. Let's get started.",
        'willkommen_2': "We have six career levels. You start as a Representative. At one thousand units, you become Lead Representative. Then it goes through Senior Representative, Coordinator, Director, all the way up to General Representative. Each level gives you a higher commission per unit. Promotions happen automatically once you meet the criteria.",
        'willkommen_3': "And now the most important question: why are you here? What's your vision? Write down in two or three sentences what drives you. That's your anchor for the days when things get tough. The app will remind you of it regularly.",
        'willkommen_4': "Your AI mentor is by your side. It gives you daily recommendations, helps coach your partners, writes weekly briefings — and answers your questions around sales. You'll find it any time in the top right under the speech bubble icon.",
        'willkommen_5': "That's it for the quickstart. Now I'll show you the cockpit live. Click Next, and I'll guide you through the most important areas. Let's go.",
        'tour_1': "This is your dashboard. At the top you see your current status: your units, your career level, your team. All numbers update live as soon as anyone enters something.",
        'tour_2': "On the left you'll find the main menu. Leads are your prospects, Contracts your closes, Team your structure. In the Coaching section, your AI mentor helps with tips and diagnostics — and in Stats you see who's in front this week.",
        'tour_3': "Important: units only count once research has been approved. That means: a contract is not closed the moment it's signed — it has to be fully reviewed. This protects you and your partners from false promises.",
        'tour_4': "On the top right you see the submission deadline and the next basic seminar. The deadline is the third workday of the month — by then all contracts must be submitted. The basic seminar takes place the second Saturday after. Plan your appointments accordingly.",
        'tour_5': "Collecting trophies is fun. There are physical rewards: a Ferrari pin at ninety-nine units, a Montblanc pen at HREP level, a Breitling watch at Coordinator. You'll find the overview under Trophies.",
        'tour_6': "That's it for the tour. You can restart it any time via the question mark at the top. Now: get going. Enter your first lead — and we'll see you on the leaderboard.",
    },
    # ============= FRANÇAIS =============
    'fr': {
        'willkommen_1': "Content que tu sois là. Bienvenue chez NT Pro Academy. Voici ton cockpit pour tout ce qui compte dans la vente structurée — carrière, commissions, coaching, ton équipe. Dans les prochaines minutes, je vais te montrer comment ça fonctionne. Commençons.",
        'willkommen_2': "Nous avons six niveaux de carrière. Tu commences comme Représentant. À mille unités, tu deviens Représentant Principal. Puis ça continue par Représentant Senior, Coordinateur, Directeur, jusqu'à Représentant Général. Chaque niveau te donne une commission plus élevée par unité. Les promotions sont automatiques dès que tu remplis les critères.",
        'willkommen_3': "Maintenant la question la plus importante : pourquoi es-tu ici ? Quelle est ta vision ? Écris en deux ou trois phrases ce qui te motive. C'est ton ancre pour les jours difficiles. L'application te le rappellera régulièrement.",
        'willkommen_4': "Ton mentor IA est à tes côtés. Il te donne des recommandations quotidiennes, aide à coacher tes partenaires, rédige des briefings hebdomadaires — et répond à tes questions sur la vente. Tu le trouves à tout moment en haut à droite sous l'icône bulle de dialogue.",
        'willkommen_5': "C'est tout pour le démarrage rapide. Maintenant je vais te montrer le cockpit en direct. Clique sur Suivant, et je te guide à travers les zones les plus importantes. Allons-y.",
        'tour_1': "Voici ton tableau de bord. En haut tu vois ton statut actuel : tes unités, ton niveau de carrière, ton équipe. Tous les chiffres se mettent à jour en direct dès que quelqu'un saisit quelque chose.",
        'tour_2': "À gauche tu trouves le menu principal. Leads sont tes prospects, Contrats tes ventes, Équipe ta structure. Dans la section Coaching, ton mentor IA aide avec des conseils et des diagnostics — et dans les Stats tu vois qui est en tête cette semaine.",
        'tour_3': "Important : les unités ne comptent qu'une fois la recherche validée. Cela veut dire : un contrat n'est pas conclu dès qu'il est signé — il doit être entièrement vérifié. Cela te protège, toi et tes partenaires, contre les fausses promesses.",
        'tour_4': "En haut à droite tu vois la date limite de soumission et le prochain séminaire de base. La date limite est le troisième jour ouvré du mois — d'ici là tous les contrats doivent être soumis. Le séminaire de base a lieu le deuxième samedi après. Planifie tes rendez-vous en conséquence.",
        'tour_5': "Collectionner des trophées, c'est sympa. Il y a des cadeaux physiques : un pin's Ferrari à quatre-vingt-dix-neuf unités, un stylo Montblanc au niveau HREP, une montre Breitling au Coordinateur. La vue d'ensemble se trouve sous Trophées.",
        'tour_6': "C'est tout pour la visite. Tu peux la relancer à tout moment via le point d'interrogation en haut. Maintenant : c'est parti. Saisis ton premier lead — et on se voit sur le classement.",
    },
}


async def gen_one(lang, name, text):
    voice, rate = VOICES[lang]
    out_dir = os.path.join(OUT, lang)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f'{name}.mp3')
    com = edge_tts.Communicate(text, voice, rate=rate)
    await com.save(path)
    size = os.path.getsize(path) / 1024
    print(f'  ✓ {lang}/{name}.mp3 ({size:.0f} KB)')


async def main():
    selected = sys.argv[1] if len(sys.argv) > 1 else None
    langs = [selected] if selected and selected in VOICES else list(VOICES.keys())

    for lang in langs:
        voice, rate = VOICES[lang]
        print(f'\n🎙  {lang.upper()}: {voice} ({rate})')
        for name, text in TEXTS[lang].items():
            await gen_one(lang, name, text)

    total = sum(len(TEXTS[l]) for l in langs)
    print(f'\n✅ Fertig. {total} MP3s in {len(langs)} Sprache(n) generiert.')


if __name__ == '__main__':
    asyncio.run(main())
