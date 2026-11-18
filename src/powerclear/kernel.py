
def process(record):
    try:
        v = float(record.get("value") or 0)
    except (TypeError, ValueError):
        v = 0.0
    scale = 1
    while abs(v) >= 1e6 and scale <= 10 ** 9:
        v /= 1000.0
        scale *= 1000
    return {"value_norm": round(v, 4), "scale_factor": scale, "was_large": int(scale > 1)}
