def total_return(initial_value: float, current_value: float) -> float:
    if initial_value <= 0: raise ValueError("initial_value must be positive")
    return current_value / initial_value - 1

