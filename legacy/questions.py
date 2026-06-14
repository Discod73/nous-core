#!/usr/bin/env python3
"""
NOUS Legacy — spørgsmålsbank til Far-interview.
~150 personlige, åbne spørgsmål fordelt på 6 kategorier.
"""

QUESTION_BANK = [
    # BARNDOM OG OPVÆKST
    {"id": "c001", "category": "barndom", "question": "Hvad er dit tidligste klare minde fra barndommen?"},
    {"id": "c002", "category": "barndom", "question": "Hvem var den voksen der betød mest for dig da du voksede op, og hvorfor?"},
    {"id": "c003", "category": "barndom", "question": "Hvad lavede du i fritiden som barn — hvad kunne du blive helt opslugt af?"},
    {"id": "c004", "category": "barndom", "question": "Hvad var den vildeste eller dummeste ting du lavede som ung?"},
    {"id": "c005", "category": "barndom", "question": "Hvad drømte du om at blive da du var lille?"},
    {"id": "c006", "category": "barndom", "question": "Hvad var dit yndlingssted som barn — et sted du altid følte dig tryg?"},
    {"id": "c007", "category": "barndom", "question": "Hvad er dit bedste minde fra skolen?"},
    {"id": "c008", "category": "barndom", "question": "Var der noget du var bange for som barn, som du i dag griner af?"},
    {"id": "c009", "category": "barndom", "question": "Hvad lavede din familie til sommer — hvad var den typiske sommerferie?"},
    {"id": "c010", "category": "barndom", "question": "Hvem var din bedste ven i folkeskolen og hvad lavede I sammen?"},
    {"id": "c011", "category": "barndom", "question": "Hvornår indså du første gang at du var ved at blive voksen?"},
    {"id": "c012", "category": "barndom", "question": "Hvad var juleaften eller en anden højtid typisk som hos jer?"},
    {"id": "c013", "category": "barndom", "question": "Hvad var den første ting du nogensinde tjente penge på?"},
    {"id": "c014", "category": "barndom", "question": "Hvad er du taknemmelig for fra din opvækst?"},
    {"id": "c015", "category": "barndom", "question": "Hvad manglede du i din barndom som du gerne ville have givet dine egne børn?"},

    # VÆRDIER OG LIVSFILOSOFI
    {"id": "v001", "category": "vaerdier", "question": "Hvad er de tre ting der betyder allermest for dig i livet?"},
    {"id": "v002", "category": "vaerdier", "question": "Hvad er din definition på et godt liv?"},
    {"id": "v003", "category": "vaerdier", "question": "Hvad ville du aldrig gå på kompromis med, uanset hvad?"},
    {"id": "v004", "category": "vaerdier", "question": "Hvad har du lært om penge og hvad de egentlig er værd?"},
    {"id": "v005", "category": "vaerdier", "question": "Hvad mener du er forskellen på at være god og at gøre det gode?"},
    {"id": "v006", "category": "vaerdier", "question": "Hvad er du mest stolt af i dit liv indtil nu?"},
    {"id": "v007", "category": "vaerdier", "question": "Hvad er den fejl du er mest taknemmelig for at have lavet?"},
    {"id": "v008", "category": "vaerdier", "question": "Hvad ville du gøre anderledes hvis du kunne starte forfra?"},
    {"id": "v009", "category": "vaerdier", "question": "Hvad er dit forhold til religon eller tro?"},
    {"id": "v010", "category": "vaerdier", "question": "Hvad mener du om at tilgive — er der grænser for hvad man bør tilgive?"},
    {"id": "v011", "category": "vaerdier", "question": "Hvornår er det okay at bryde et løfte?"},
    {"id": "v012", "category": "vaerdier", "question": "Hvad er forskellen på at være lykkelig og at have det godt?"},
    {"id": "v013", "category": "vaerdier", "question": "Hvad ville du fortælle dit 20-årige jeg, hvis du kunne?"},
    {"id": "v014", "category": "vaerdier", "question": "Hvad er dit forhold til ensomhed — er det noget man skal flygte fra?"},
    {"id": "v015", "category": "vaerdier", "question": "Hvad er den vigtigste lektion dit arbejdsliv har lært dig?"},
    {"id": "v016", "category": "vaerdier", "question": "Hvad er din holdning til at bede om hjælp?"},
    {"id": "v017", "category": "vaerdier", "question": "Hvad mener du er meningen med at leve?"},
    {"id": "v018", "category": "vaerdier", "question": "Hvad har du lært om venskab over tid?"},
    {"id": "v019", "category": "vaerdier", "question": "Hvornår er det modfigt at sige fra — og hvornår er det sejt?"},
    {"id": "v020", "category": "vaerdier", "question": "Hvad er dit råd om at håndtere modgang?"},

    # PERSONLIGHED OG HUMOR
    {"id": "p001", "category": "personlighed", "question": "Hvordan vil dine venner beskrive dig med tre ord — og er de rigtige?"},
    {"id": "p002", "category": "personlighed", "question": "Hvad er noget folk misforstår ved dig?"},
    {"id": "p003", "category": "personlighed", "question": "Hvad er den sjoveste ting der nogensinde er sket dig?"},
    {"id": "p004", "category": "personlighed", "question": "Hvad gør dig vred — og hvad gør dig glad?"},
    {"id": "p005", "category": "personlighed", "question": "Hvad er din supersans — den ting du er usædvanligt god til at mærke eller forstå?"},
    {"id": "p006", "category": "personlighed", "question": "Hvad er din største svaghed, og har du lært at elske den?"},
    {"id": "p007", "category": "personlighed", "question": "Hvad er du urealistisk god til?"},
    {"id": "p008", "category": "personlighed", "question": "Hvad er du urealistisk dårlig til, og hvad siger det om dig?"},
    {"id": "p009", "category": "personlighed", "question": "Hvad er din yndlingsjoke, eller hvad er den ting der altid får dig til at grine?"},
    {"id": "p010", "category": "personlighed", "question": "Hvad er den ting du har arbejdet hårdest på at ændre ved dig selv?"},
    {"id": "p011", "category": "personlighed", "question": "Hvad er den ting ved dig selv som du aldrig vil ændre?"},
    {"id": "p012", "category": "personlighed", "question": "Hvad giver dig energi — hvad dræner dig?"},
    {"id": "p013", "category": "personlighed", "question": "Hvad er dit forhold til regler — følger du dem, eller finder du genveje?"},
    {"id": "p014", "category": "personlighed", "question": "Hvad nyder du at gøre helt alene?"},
    {"id": "p015", "category": "personlighed", "question": "Hvad er den ting du er mest jaloux på hos andre — og hvad siger det om dig?"},

    # HISTORIER OG ANEKDOTER
    {"id": "h001", "category": "historier", "question": "Fortæl om en dag der ændrede alt — stor eller lille."},
    {"id": "h002", "category": "historier", "question": "Hvad er den mest absurde situation du nogensinde har befundet dig i?"},
    {"id": "h003", "category": "historier", "question": "Hvad er den bedste beslutning du har truffet i dit liv?"},
    {"id": "h004", "category": "historier", "question": "Hvornår var du mest bange, og hvad lærte du af det?"},
    {"id": "h005", "category": "historier", "question": "Fortæl om et øjeblik du er virkelig stolt af — noget ingen andre ved."},
    {"id": "h006", "category": "historier", "question": "Hvad er den vildeste ting du har oplevet i dit arbejdsliv?"},
    {"id": "h007", "category": "historier", "question": "Fortæl om en person der overraskede dig totalt — på god eller dårlig vis."},
    {"id": "h008", "category": "historier", "question": "Hvad er den rejse eller oplevelse der har sat dybest aftryk i dig?"},
    {"id": "h009", "category": "historier", "question": "Fortæl om en gang du hjalp nogen — og det betød mere end du troede."},
    {"id": "h010", "category": "historier", "question": "Hvad er den bedste gave du nogensinde har fået, stor eller lille?"},
    {"id": "h011", "category": "historier", "question": "Fortæl om en gang du var komplet uden for din komfortzone."},
    {"id": "h012", "category": "historier", "question": "Hvad er den fejl du aldrig glemmer — og hvad lærte du?"},
    {"id": "h013", "category": "historier", "question": "Fortæl om en gang du stod op for noget du troede på, selvom det kostede noget."},
    {"id": "h014", "category": "historier", "question": "Hvad er det sjoveste sammenfald eller den vildeste tilfældighed der er sket i dit liv?"},
    {"id": "h015", "category": "historier", "question": "Fortæl om en gang tingene gik helt skævt — og hvordan det endte."},
    {"id": "h016", "category": "historier", "question": "Hvad er det mest overraskende ved at blive ældre?"},
    {"id": "h017", "category": "historier", "question": "Hvad er den sætning en anden person har sagt til dig, som du aldrig har glemt?"},
    {"id": "h018", "category": "historier", "question": "Fortæl om den dag Gaia eller Gabriel satte dig helt ud af spillet — hvad skete der?"},
    {"id": "h019", "category": "historier", "question": "Hvad er den bedste morgen du husker — hvad gjorde den speciel?"},
    {"id": "h020", "category": "historier", "question": "Fortæl om en gang du var fuldstændig i dit es."},

    # TIL GAIA OG GABRIEL
    {"id": "g001", "category": "til_boernene", "question": "Hvad vil du have Gaia og Gabriel ved om dig som de ikke ved nu?"},
    {"id": "g002", "category": "til_boernene", "question": "Hvad er det vigtigste råd du kan give dem om kærlighed?"},
    {"id": "g003", "category": "til_boernene", "question": "Hvad håber du de aldrig glemmer fra deres barndom?"},
    {"id": "g004", "category": "til_boernene", "question": "Hvad vil du sige til dem den dag de bliver voksne?"},
    {"id": "g005", "category": "til_boernene", "question": "Hvad er dit håb for deres fremtid?"},
    {"id": "g006", "category": "til_boernene", "question": "Hvad er det sværeste ved at være far, og hvad er det bedste?"},
    {"id": "g007", "category": "til_boernene", "question": "Hvad vil du have de husker om dig, hvis du ikke er der mere?"},
    {"id": "g008", "category": "til_boernene", "question": "Hvad har du lært af at være far som ingen fortalte dig på forhånd?"},
    {"id": "g009", "category": "til_boernene", "question": "Hvad er det bedste øjeblik du har haft med Gaia alene?"},
    {"id": "g010", "category": "til_boernene", "question": "Hvad er det bedste øjeblik du har haft med Gabriel alene?"},
    {"id": "g011", "category": "til_boernene", "question": "Hvad vil du have de ved om kamp — om at stå op, blive ved, og rejse sig?"},
    {"id": "g012", "category": "til_boernene", "question": "Hvad er den ting du allerhelst vil give dem med fra dig?"},
    {"id": "g013", "category": "til_boernene", "question": "Hvad er din dybeste frygt for deres fremtid — og hvad gør du ved den?"},
    {"id": "g014", "category": "til_boernene", "question": "Hvad vil du sige til Gaia specifikt — noget kun til hende?"},
    {"id": "g015", "category": "til_boernene", "question": "Hvad vil du sige til Gabriel specifikt — noget kun til ham?"},
    {"id": "g016", "category": "til_boernene", "question": "Hvad håber du de tænker på dig om 20 år?"},
    {"id": "g017", "category": "til_boernene", "question": "Hvad er dit råd til dem om svære mennesker i livet?"},
    {"id": "g018", "category": "til_boernene", "question": "Hvad er det sværeste du har oplevet som far — og hvad lærte du?"},
    {"id": "g019", "category": "til_boernene", "question": "Hvad er din definition på at være en god far?"},
    {"id": "g020", "category": "til_boernene", "question": "Hvad er den sang, film eller bog du vil have de kender, og hvorfor?"},

    # VERDENSSYN
    {"id": "u001", "category": "verdenssyn", "question": "Hvad mener du om retfærdighed — findes den, eller skaber man den selv?"},
    {"id": "u002", "category": "verdenssyn", "question": "Hvad giver dit liv mening?"},
    {"id": "u003", "category": "verdenssyn", "question": "Hvad tror du sker efter døden?"},
    {"id": "u004", "category": "verdenssyn", "question": "Hvad er du bange for at verden mister?"},
    {"id": "u005", "category": "verdenssyn", "question": "Hvad er du optimist på vegne af?"},
    {"id": "u006", "category": "verdenssyn", "question": "Hvad mener du om AI — er det godt, farligt, eller begge dele?"},
    {"id": "u007", "category": "verdenssyn", "question": "Hvad er din holdning til Danmark som samfund — hvad er vi gode til og dårlige til?"},
    {"id": "u008", "category": "verdenssyn", "question": "Hvad mener du om det danske retssystem?"},
    {"id": "u009", "category": "verdenssyn", "question": "Hvad er dit forhold til naturen?"},
    {"id": "u010", "category": "verdenssyn", "question": "Hvad tror du er det største problem menneskeheden skal løse?"},
    {"id": "u011", "category": "verdenssyn", "question": "Hvad er din holdning til at leve nu kontra at planlægge fremtiden?"},
    {"id": "u012", "category": "verdenssyn", "question": "Hvad betyder frihed for dig?"},
    {"id": "u013", "category": "verdenssyn", "question": "Hvad er din oplevelse af at stå alene mod systemet?"},
    {"id": "u014", "category": "verdenssyn", "question": "Hvad mener du er den vigtigste menneskelige egenskab?"},
    {"id": "u015", "category": "verdenssyn", "question": "Hvad er dit forhold til tid — har du altid nok, eller altid for lidt?"},
]

CATEGORY_LABELS: dict[str, str] = {
    "barndom":      "Barndom og opvækst",
    "vaerdier":     "Værdier og livsfilosofi",
    "personlighed": "Personlighed og humor",
    "historier":    "Historier og anekdoter",
    "til_boernene": "Til Gaia og Gabriel",
    "verdenssyn":   "Verdenssyn",
}

# Kategorier der prioriteres i dagligt spørgsmål
PRIORITY_CATEGORIES = frozenset({"til_boernene", "vaerdier"})


def all_ids() -> list[str]:
    return [q["id"] for q in QUESTION_BANK]


def by_id(question_id: str) -> dict | None:
    return next((q for q in QUESTION_BANK if q["id"] == question_id), None)


def by_category(category: str) -> list[dict]:
    return [q for q in QUESTION_BANK if q["category"] == category]
