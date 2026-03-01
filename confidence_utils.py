def confidence_label(score):
    if score >= 0.95:
        return "✅ High"
    elif score >= 0.85:
        return "⚠️ Medium"
    return "❌ Review"
