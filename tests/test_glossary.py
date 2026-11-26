"""Глоссарий без модели: определения из книги, таблица обозначений, английские термины автора, слияние дублей."""
from g2c.glossary import BRACKET_EN_RE, _merge_abbr_duplicates, find_definition


def test_definition_must_be_a_definition():
    texts = [(90, "Водоотдача - основной показатель качества глинистого раствора; чем ниже водоотдача, тем лучше раствор."),
             (18, "Для коллектора изменение сопротивления в зоне проникновения есть отношение двух величин.")]
    assert find_definition("водоотдача", texts) == (texts[0][1], 90)
    assert find_definition("зона проникновения", texts) is None      # «…в зоне проникновения есть…» — не определение
    assert find_definition("зонд БК-3", [(17, "Эта зона называется промытым пластом.")]) is None


def test_definition_patterns():
    assert find_definition("коллектор", [(5, "Коллекторами называют горные породы, способные вмещать и отдавать флюиды.")])
    assert find_definition("пористость", [(5, "Под пористостью понимают долю пустот в объёме породы.")])


def test_bilingual_pairs_from_author_text():
    m = BRACKET_EN_RE.search("1.2.2 Преимущественно волновые дельты [Wave-dominated deltas] и …")
    assert m and m.group(2) == "Wave-dominated deltas"


def test_merge_abbreviation_duplicates():
    entries = [{"term": "ВНК", "abbr": "ВНК", "definition": ""},
               {"term": "водонефтяной контакт", "abbr": "ВНК", "definition": "", "aliases": []}]
    out = _merge_abbr_duplicates(entries)
    assert len(out) == 1 and out[0]["term"] == "водонефтяной контакт" and "ВНК" in out[0]["aliases"]
