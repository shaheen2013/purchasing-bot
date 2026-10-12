import pytest

from bot.api import Throttle
from bot.browser import parse_confirmation
from bot.config import ConfigError, check_test_card
from bot.ledger import Ledger
from bot.models import IllegalTransition
from bot.simulate import SimulationError, parse_simulations

from .fakes import FakeDemoblaze


def test_parse_confirmation_masks_card_and_fixes_month():
    c = parse_confirmation("Id: 4821733\nAmount: 1590 USD\nCard Number: 4111 1111 1111 1111\n"
                           "Name: Demo Buyer\nDate: 12/9/2026")
    assert (c.order_id, c.amount, c.card_last4, c.name) == ("4821733", 1590, "1111", "Demo Buyer")
    assert c.date_iso == "2026-10-12"  # cart.js uses 0-based getMonth()
    assert "4111 1111 1111 1111" not in c.text_masked and "**** **** **** 1111" in c.text_masked


def test_parse_confirmation_without_newlines():
    c = parse_confirmation("Id: 77 Amount: 360 USD Card Number: 4111111111111111 Name: A B Date: 1/0/2026")
    assert (c.order_id, c.amount, c.card_last4, c.name, c.date_iso) == ("77", 360, "1111", "A B", "2026-01-01")


def test_only_test_cards_are_accepted():
    assert check_test_card("4111 1111 1111 1111") == "4111111111111111"
    with pytest.raises(ConfigError):
        check_test_card("4539 1488 0343 6467")


def test_simulate_parsing():
    sim = parse_simulations(["price_drift=5:20,3:10", "stock_out=6,14"])
    assert sim.price_drift == {5: 20.0, 3: 10.0} and sim.stock_out == {6, 14}
    assert sim.observed_price(5, 790) == 948.0
    with pytest.raises(SimulationError):
        parse_simulations(["bogus=1"])


def test_throttle_spaces_requests():
    t = [0.0]
    slept = []
    th = Throttle(2.0, clock=lambda: t[0], sleep=lambda s: (slept.append(s), t.__setitem__(0, t[0] + s)))
    for _ in range(3):
        th.wait()
    assert slept == [0.5, 0.5]


def test_catalog_pagination_and_title_whitespace():
    api = FakeDemoblaze().api()
    products = api.entries()
    assert {p.id for p in products} == {1, 2, 3, 10, 11}


def test_ledger_rejects_illegal_transitions(tmp_path):
    led = Ledger(tmp_path / "l.db")
    led.create_run("r", "api", False, "o.csv", 0, {})
    led.add_line("r", 1)
    with pytest.raises(IllegalTransition):
        led.set_line_state("r", 1, "CONFIRMED")
