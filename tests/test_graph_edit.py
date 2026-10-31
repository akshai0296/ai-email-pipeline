"""Hand edits to brand and product graphs: validation, branches, deletes that stick, logos and photos."""

import asyncio
import io

import brand_graph as bg
import graph_edit as ge
import product_graph as pg
import pytest
from PIL import Image, ImageDraw

BRAND = "acme-com"
PRODUCT = "tree-runner"


@pytest.fixture
def brand(storage):
    bg.save({"id": BRAND, "nodes": [{"id": "company", "type": "Company", "name": "Acme"}], "edges": []})
    return BRAND


@pytest.fixture
def product(brand):
    pg.save({
        "id": PRODUCT, "brand_id": BRAND, "url": "https://acme.example/tree-runner",
        "nodes": [
            {"id": "product", "type": "Product", "name": "Tree Runner"},
            {"id": "image:hero", "type": "ProductImage", "role": "hero", "file": f"brands/{BRAND}/products/{PRODUCT}/image-0.png"},
        ],
        "edges": [{"from": "product", "rel": "HAS_IMAGE", "to": "image:hero"}],
    })
    return PRODUCT


def png(draw=None, size=(200, 80), background=(255, 255, 255, 255)) -> bytes:
    img = Image.new("RGBA", size, background)
    if draw:
        draw(ImageDraw.Draw(img))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_add_color_normalises_hex_and_replaces_the_old_one(brand):
    ge.add_node(BRAND, None, "Color", {"role": "accent", "hex": "1a2b3c"})
    node = ge.add_node(BRAND, None, "Color", {"role": "accent", "hex": "#ffcc00"})
    graph = bg.load(BRAND)
    assert node == {"id": "color:accent", "type": "Color", "role": "accent", "hex": "#FFCC00", "edited": True, "source": "edited by you"}
    assert [n["hex"] for n in bg.nodes(graph, "Color")] == ["#FFCC00"]
    assert graph["edges"] == [{"from": "company", "rel": "HAS_COLOR", "to": "color:accent"}]


@pytest.mark.parametrize(
    ("type_", "fields", "message"),
    [
        ("Color", {"role": "accent", "hex": "red"}, "6-digit hex"),
        ("Color", {"role": "border", "hex": "#000000"}, "color role"),
        ("Link", {"kind": "shop", "url": "ftp://acme.example"}, "https://"),
        ("Rule", {"text": "  "}, "fill in text"),
        ("Company", {"name": "Other"}, "can't add a Company"),
        ("Logo", {"background": "light"}, "can't add a Logo"),
    ],
)
def test_add_rejects_bad_input(brand, type_, fields, message):
    with pytest.raises(ge.EditError, match=message):
        ge.add_node(BRAND, None, type_, fields)


def test_fields_outside_the_type_are_dropped(brand):
    node = ge.add_node(BRAND, None, "Rule", {"text": "Never say cheap", "id": "company", "edited": False})
    assert node["id"].startswith("rule:yours-") and node["edited"] is True


def test_a_fact_hangs_from_its_topic_branch(brand):
    fact = ge.add_node(BRAND, None, "Fact", {"topic": "Sustainability", "text": "Carbon neutral since 2019"})
    graph = bg.load(BRAND)
    assert bg.node(graph, "topic:sustainability")["name"] == "Sustainability"
    assert {"from": "company", "rel": "HAS_TOPIC", "to": "topic:sustainability"} in graph["edges"]
    assert {"from": "topic:sustainability", "rel": "INCLUDES", "to": fact["id"]} in graph["edges"]


def test_moving_a_fact_relinks_it_and_renaming_a_topic_updates_its_facts(brand):
    fact = ge.add_node(BRAND, None, "Fact", {"topic": "Sustainability", "text": "Carbon neutral"})
    ge.update_node(BRAND, None, fact["id"], {"topic": "Materials"})
    graph = bg.load(BRAND)
    assert [e["from"] for e in graph["edges"] if e["to"] == fact["id"]] == ["topic:materials"]

    ge.update_node(BRAND, None, "topic:materials", {"name": "What it's made of"})
    assert bg.node(bg.load(BRAND), fact["id"])["topic"] == "What it's made of"


def test_deleting_a_topic_takes_its_facts_and_stays_deleted(brand):
    fact = ge.add_node(BRAND, None, "Fact", {"topic": "Sustainability", "text": "Carbon neutral"})
    ge.delete_node(BRAND, None, "topic:sustainability")
    graph = bg.load(BRAND)
    assert not bg.node(graph, "topic:sustainability") and not bg.node(graph, fact["id"])
    assert all("topic:sustainability" not in (e["from"], e["to"]) for e in graph["edges"])
    assert set(graph["deleted_by_user"]) == {"topic:sustainability", fact["id"]}
    assert set(graph["deleted_keys"]) == {"Topic:sustainability", "Fact:carbonneutral"}


def test_adding_it_back_clears_the_deleted_marker(brand):
    ge.add_node(BRAND, None, "Voice", {"text": "Warm and plain"})
    ge.delete_node(BRAND, None, "voice")
    ge.add_node(BRAND, None, "Voice", {"text": "Warm and plain"})
    graph = bg.load(BRAND)
    assert "voice" not in graph["deleted_by_user"] and bg.node(graph, "voice")


def test_the_center_node_cannot_be_deleted(brand):
    with pytest.raises(ge.EditError):
        ge.delete_node(BRAND, None, "company")
    with pytest.raises(KeyError):
        ge.delete_node(BRAND, None, "rule:missing")


def test_product_graphs_take_their_own_types(product):
    feature = ge.add_node(BRAND, PRODUCT, "Feature", {"text": "Machine washable"})
    graph = pg.load(BRAND, PRODUCT)
    assert {"from": "product", "rel": "HAS_FEATURE", "to": feature["id"]} in graph["edges"]
    with pytest.raises(ge.EditError):
        ge.add_node(BRAND, PRODUCT, "Address", {"text": "1 Main St"})


def test_logo_mode_needs_a_product_logo(product):
    with pytest.raises(ge.EditError, match="logo mode is one of"):
        ge.set_logo_mode(BRAND, PRODUCT, "sideways")
    with pytest.raises(ge.EditError, match="upload a logo"):
        ge.set_logo_mode(BRAND, PRODUCT, "product")


def test_topic_id():
    assert ge.topic_id("  Fit & Sizing! ") == "topic:fit-sizing"
    with pytest.raises(ge.EditError):
        ge.topic_id("!!!")


def test_cut_off_sides_flags_ink_on_one_edge_only():
    centered = Image.open(io.BytesIO(png(lambda d: d.rectangle((40, 20, 160, 60), fill="black"))))
    cut_left = Image.open(io.BytesIO(png(lambda d: d.rectangle((0, 20, 120, 60), fill="black"))))
    full = Image.open(io.BytesIO(png(lambda d: d.rectangle((0, 0, 199, 79), fill="black"))))
    assert ge.cut_off_sides(centered) == []
    assert ge.cut_off_sides(cut_left) == ["left"]
    assert ge.cut_off_sides(full) == []


def test_upload_logo_saves_the_file_and_warns_when_cut_off(brand, storage):
    result = asyncio.run(ge.upload_logo(BRAND, None, png(lambda d: d.rectangle((0, 20, 120, 60), fill="black")), "logo.png"))
    (logo,) = result["logos"]
    assert logo["id"] == "logo:primary" and logo["edited"] is True
    assert (storage / logo["file"]).exists()
    assert "cut off at the left edge" in result["warning"]
    assert {"from": "company", "rel": "HAS_LOGO", "to": "logo:primary"} in bg.load(BRAND)["edges"]


@pytest.mark.parametrize(("data", "message"), [(b"not an image", "isn't an image"), (png(size=(20, 10)), "too small")])
def test_upload_logo_rejects_bad_files(brand, data, message):
    with pytest.raises(ge.EditError, match=message):
        asyncio.run(ge.upload_logo(BRAND, None, data, "logo.png"))


def test_a_new_hero_photo_goes_first_and_demotes_the_old_one(product, storage):
    staged = f"brands/{BRAND}/products/{PRODUCT}/proposed/found.png"
    (storage / staged).parent.mkdir(parents=True)
    (storage / staged).write_bytes(png(size=(64, 48)))
    node = ge.add_product_image(BRAND, PRODUCT, {"file": staged, "role": "hero", "description": "On foot"}, "found by Modify")
    photos = bg.nodes(pg.load(BRAND, PRODUCT), "ProductImage")
    assert [p["id"] for p in photos] == [node["id"], "image:hero"]
    assert photos[1]["role"] == "product"
    assert node["file"].endswith("image-1.png") and (node["width"], node["height"]) == (64, 48)


def test_product_photos_must_come_from_the_staging_folder(product, storage):
    with pytest.raises(ge.EditError, match="no longer available"):
        ge.add_product_image(BRAND, PRODUCT, {"file": "brands/other/secret.png"}, "found by Modify")
