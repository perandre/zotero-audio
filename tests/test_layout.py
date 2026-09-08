from zotero_audio.layout import reading_order, line_text


def test_two_columns_are_read_completely_before_switching():
    def box(x, y, label):
        return dict(x0=x, x1=x+240, y0=y, y1=y+10, label=label)
    boxes = [box(30, 500, "6"), box(310, 300, "6.1.1"),
             box(30, 650, "6.1"), box(310, 700, "6.2")]
    assert [b["label"] for b in reading_order(boxes, 600)] == ["6", "6.1", "6.1.1", "6.2"]


def test_full_width_table_separates_column_regions():
    boxes = [dict(x0=310,x1=560,y0=20,y1=50), dict(x0=30,x1=560,y0=100,y1=200),
             dict(x0=30,x1=280,y0=210,y1=300), dict(x0=30,x1=280,y0=20,y1=50)]
    assert reading_order(boxes, 600) == [boxes[3],boxes[0],boxes[1],boxes[2]]


def test_original_soft_hyphen_and_font_boundary_spacing_are_preserved():
    line = {"spans": [{"text":"organi\u00ad", "bbox":[0,0,20,10]},
                      {"text":"word", "bbox":[22,0,40,10]}]}
    assert line_text(line) == "organi\u00ad word"
