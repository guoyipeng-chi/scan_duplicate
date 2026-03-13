double normalize_value(double input, double low, double high) {
    double denominator = high - low;
    if (denominator <= 0.0) {
        return 0.0;
    }

    double normalized = (input - low) / denominator;
    if (normalized < 0.0) {
        normalized = 0.0;
    }
    if (normalized > 1.0) {
        normalized = 1.0;
    }

    double scaled = normalized * 100.0;
    if (scaled < 0.0) {
        scaled = 0.0;
    }
    if (scaled > 100.0) {
        scaled = 100.0;
    }
    return scaled;
}

double clamp_value(double input, double low, double high) {
    double denominator = high - low;
    if (denominator <= 0.0) {
        return 0.0;
    }

    double normalized = (input - low) / denominator;
    if (normalized < 0.0) {
        normalized = 0.0;
    }
    if (normalized > 1.0) {
        normalized = 1.0;
    }

    double scaled = normalized * 100.0;
    if (scaled < 0.0) {
        scaled = 0.0;
    }
    if (scaled > 100.0) {
        scaled = 100.0;
    }
    return scaled;
}
