import pytest
from sanctum.sanctum import apply_overrides, build_parser

def test_apply_overrides_basic():
    config = {"a": 1, "b": {"c": 2}}
    overrides = ["a=10", "b.c=20", "d=30"]
    new_config = apply_overrides(config, overrides)
    assert new_config["a"] == 10
    assert new_config["b"]["c"] == 20
    assert new_config["d"] == 30

def test_apply_overrides_types():
    config = {}
    overrides = [
        "int_val=10",
        "float_val=10.5",
        "bool_true=true",
        "bool_false=false",
        "string_val=hello"
    ]
    new_config = apply_overrides(config, overrides)
    assert new_config["int_val"] == 10
    assert isinstance(new_config["int_val"], int)
    assert new_config["float_val"] == 10.5
    assert isinstance(new_config["float_val"], float)
    assert new_config["bool_true"] is True
    assert new_config["bool_false"] is False
    assert new_config["string_val"] == "hello"

def test_apply_overrides_nested():
    config = {"nested": {"deep": {"value": 1}}}
    overrides = ["nested.deep.value=100", "nested.new_key=200"]
    new_config = apply_overrides(config, overrides)
    assert new_config["nested"]["deep"]["value"] == 100
    assert new_config["nested"]["new_key"] == 200

def test_parser_subcommands():
    parser = build_parser()
    
    # Init
    args = parser.parse_args(["init"])
    assert args.command == "init"
    
    # Watchlist
    args = parser.parse_args(["watchlist", "add", "AAPL"])
    assert args.command == "watchlist"
    assert args.action == "add"
    assert args.ticker == "AAPL"
    
    # Portfolio
    args = parser.parse_args(["portfolio", "add", "MSFT", "10", "300.5"])
    assert args.command == "portfolio"
    assert args.action == "add"
    assert args.ticker == "MSFT"
    assert args.shares == 10.0
    assert args.avg_cost == 300.5
    
    # Screen
    args = parser.parse_args(["screen", "--tickers", "AAPL,MSFT", "--export", "pdf"])
    assert args.command == "screen"
    assert args.tickers == "AAPL,MSFT"
    assert args.export == "pdf"
    
    # Analyze
    args = parser.parse_args(["analyze", "TSLA"])
    assert args.command == "analyze"
    assert args.ticker == "TSLA"

def test_parser_overrides():
    parser = build_parser()
    args = parser.parse_args(["--set", "wacc.rf=0.05", "--set", "dcf.growth=0.03", "screen"])
    assert args.overrides == ["wacc.rf=0.05", "dcf.growth=0.03"]
    assert args.command == "screen"
