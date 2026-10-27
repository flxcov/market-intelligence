import importlib.util

# Dashboard tests need the optional [dashboard] extra (Streamlit).
if importlib.util.find_spec("streamlit") is None:
    collect_ignore_glob = ["test_*.py"]
