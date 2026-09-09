from zotero_audio.layout import reading_order, line_text, report_line_text


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


def test_report_footnotes_do_not_change_figures_or_unit_exponents():
    def line(prefix):
        return {"spans": [{"text": prefix, "bbox": [0, 0, 20, 10], "flags": 0},
                          {"text": "2", "bbox": [20, 0, 23, 6], "flags": 1},
                          {"text": " of the total", "bbox": [23, 0, 80, 10], "flags": 0}]}
    assert report_line_text(line("80%")) == "80% of the total"
    assert report_line_text(line("$17Bn")) == "$17Bn of the total"
    assert report_line_text(line("m")) == "m2 of the total"
    assert line_text(line("80%")) == "80%2 of the total"


def test_report_three_columns_are_read_in_full_before_switching():
    boxes = [dict(x0=x, x1=x+220, y0=y, y1=y+20, label=label)
             for x, y, label in [(310, 200, "middle-end"), (30, 20, "left-start"),
                                 (590, 20, "right-start"), (30, 200, "left-end"),
                                 (310, 20, "middle-start"), (590, 200, "right-end")]]
    assert [b["label"] for b in reading_order(boxes, 960, multi_column=True)] == [
        "left-start", "left-end", "middle-start", "middle-end", "right-start", "right-end"]


def test_report_sidebar_spanning_two_columns_follows_all_three_columns():
    boxes = [dict(x0=x, x1=x+w, y0=y, y1=y+50, label=label)
             for x, y, w, label in [(130, 200, 500, "left"), (700, 200, 500, "middle"),
                                    (1270, 200, 500, "right"), (700, 650, 1060, "sidebar")]]
    assert [b["label"] for b in reading_order(boxes, 1920, multi_column=True)] == ["left", "middle", "right", "sidebar"]


def test_slide_report_labels_stay_with_their_explanatory_rows():
    boxes = [dict(x0=45, x1=160, y0=130, y1=210, label="first-label", boxclass="section-header"),
             dict(x0=45, x1=160, y0=278, y1=346, label="second-label", boxclass="section-header"),
             dict(x0=210, x1=910, y0=120, y1=181, label="first-body"),
             dict(x0=210, x1=910, y0=235, y1=260, label="second-body")]
    assert [b["label"] for b in reading_order(boxes, 960, multi_column=True)] == [
        "first-label", "first-body", "second-label", "second-body"]
