#!/usr/bin/env python3
"""Create a synthetic messages database for testing external LLM APIs.

All people, groups and messages are made up. The database carries a
`synthetic` marker table; the summarizer only sends chats to external
APIs from marked databases (unless --allow-real-data is given).

Usage: python sample_data.py /samples/sample.db
"""

import sqlite3
import sys
import time

# Same schema as the bridge creates (bridge/store.go), plus the marker table.
SCHEMA = """
CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT);
CREATE TABLE messages (
    chat_jid TEXT NOT NULL, id TEXT NOT NULL, sender_jid TEXT, sender_name TEXT,
    ts INTEGER NOT NULL, text TEXT NOT NULL, from_me INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_jid, id)
);
CREATE INDEX idx_messages_chat_ts ON messages (chat_jid, ts);
CREATE TABLE synthetic (note TEXT);
"""

# (group name, [(minutes before now, sender, text), ...])
CHATS = [
    ("Klasa 2c (test)", [
        (1310, "Marta Zielińska", "Dzień dobry! Przypominam, że w czwartek 6.11 wycieczka do Centrum Nauki Experyment."),
        (1302, "Marta Zielińska", "Zbiórka 8:00 pod szkołą, powrót ok. 14:30. Koszt 65 zł, proszę wpłacać do poniedziałku."),
        (1290, "Tomasz Wrona", "Czy trzeba dać dzieciom prowiant czy jest obiad na miejscu?"),
        (1281, "Marta Zielińska", "Prowiant własny, obiadu nie ma. Dobrze dać coś do picia."),
        (1250, "Kasia Nowak", "Dzięki!"),
        (1248, "Kasia Nowak", "[sticker]"),
        (980, "Piotr Lis", "Czy ktoś wie, co było zadane z matematyki? Zuzia zapomniała zapisać."),
        (972, "Agnieszka Bąk", "Ćwiczenia str. 34, zadania 1-3."),
        (970, "Agnieszka Bąk", "[image]"),
        (965, "Piotr Lis", "Super, dziękuję 🙏"),
        (640, "Marta Zielińska", "Ważne: zebranie z rodzicami przesunięte z wtorku na środę 12.11 o 17:30, sala 14."),
        (631, "Tomasz Wrona", "Czy będzie można dołączyć online?"),
        (622, "Marta Zielińska", "Niestety tylko stacjonarnie."),
        (300, "Ewa Krawczyk", "Zgubiona niebieska bluza z jednorożcem, rozmiar 128. Jeśli ktoś znajdzie, proszę o info."),
        (288, "Kasia Nowak", "Widziałam podobną w szatni przy sali gimnastycznej."),
        (120, "Ewa Krawczyk", "Znalazła się, dziękuję!"),
    ]),
    ("Rodzice 4a (test)", [
        (1400, "Joanna Mazur", "Kochani, zbieramy na prezent dla pani Ani na Dzień Nauczyciela. Propozycja 25 zł od dziecka."),
        (1390, "Robert Kowal", "OK, ja wpłacam dziś."),
        (1385, "Beata Szymańska", "Czy kwiaty też z tego?"),
        (1380, "Joanna Mazur", "Tak, kwiaty plus bon do księgarni. Numer konta wyślę prywatnie."),
        (1100, "Beata Szymańska", "Przypominam, że w piątek dzień sportu, dzieci w strojach sportowych od rana."),
        (1090, "Michał Dudek", "A co z obuwiem? Halówki czy zwykłe adidasy?"),
        (1085, "Beata Szymańska", "Halówki z jasną podeszwą."),
        (700, "Robert Kowal", "[voice note]"),
        (690, "Joanna Mazur", "Do tej pory wpłaciło 14 z 22 osób. Termin do środy."),
        (500, "Michał Dudek", "Czy ktoś organizuje dowóz na zawody pływackie w sobotę? Mam 2 wolne miejsca w aucie."),
        (480, "Anna Lewandowska", "Chętnie, jeśli Antek może się zabrać. Wyjazd o której?"),
        (470, "Michał Dudek", "8:15 spod szkoły."),
        (90, "Joanna Mazur", "Wszystkie wpłaty zebrane, dziękuję! Prezent kupiony."),
    ]),
    ("Wyjazd na narty (test)", [
        (1350, "Krzysiek", "To co, ferie: Białka czy Szczyrk? Głosujemy do niedzieli."),
        (1340, "Ola", "Białka, bliżej do term 😄"),
        (1335, "Tomek", "Szczyrk, taniej i nowe wyciągi."),
        (1200, "Krzysiek", "[poll] Gdzie jedziemy? Białka / Szczyrk"),
        (1100, "Łukasz", "Ja mogę tylko w drugim tygodniu ferii, 26.01-1.02."),
        (1090, "Ola", "Nam też drugi tydzień pasuje."),
        (800, "Krzysiek", "Znalazłem apartament w Białce dla 8 osób, 3200 zł za tydzień. Rezerwujemy?"),
        (790, "Tomek", "Jeśli Białka wygra, to biorę. Zaliczka?"),
        (780, "Krzysiek", "30% zaliczki do piątku, czyli 120 zł od osoby."),
        (400, "Ola", "Kto ma bagażnik dachowy? Nie zmieścimy nart."),
        (390, "Łukasz", "Mam boks, mogę pożyczyć."),
        (60, "Krzysiek", "Głosowanie zamknięte: Białka 5, Szczyrk 2. Rezerwuję apartament, proszę o zaliczki."),
    ]),
]


def create_sample_db(path, now):
    try:
        open(path, "x").close()  # never overwrite a real database by accident
    except FileExistsError:
        raise FileExistsError(f"{path} already exists") from None
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO synthetic VALUES ('made-up test data, safe to send to external APIs')")
    for i, (name, messages) in enumerate(CHATS, start=1):
        jid = f"1203639999999999{i:02d}@g.us"
        db.execute("INSERT INTO chats VALUES (?, ?)", (jid, name))
        for n, (minutes_ago, sender, text) in enumerate(messages):
            db.execute(
                "INSERT INTO messages (chat_jid, id, sender_jid, sender_name, ts, text) VALUES (?, ?, ?, ?, ?, ?)",
                (jid, f"TEST{i}{n:03d}", f"9999{i}{n:03d}@lid", sender, int(now - minutes_ago * 60), text),
            )
    db.commit()
    db.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    create_sample_db(sys.argv[1], time.time())
    print(f"Created {sys.argv[1]}")
