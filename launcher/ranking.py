import random

def calculate_weight(task_fit, health, remaining_fraction, hours_to_reset, promo_multiplier):
    if health <= 0 or remaining_fraction <= 0:
        return 0.0
    
    # Cap so remaining~0 cannot explode.
    # We want f to be high when remaining is high and hours_to_reset is small (impending reset).
    # But wait, if remaining~0, the quota is almost exhausted, we shouldn't boost it.
    # Ah: "with cap so remaining~0 cannot explode".
    # actually f(remaining_fraction, hours_to_reset).
    # "Grok/zai preferred when useful quota would expire" -> high remaining, low hours.
    f_reset = remaining_fraction / max(hours_to_reset, 0.01)
    cap = 5.0
    saturated_reset_bonus = 1 + min(cap, f_reset)
    
    quota_headroom = remaining_fraction # using fraction as headroom
    return task_fit * health * quota_headroom * saturated_reset_bonus * promo_multiplier

def select_candidate(candidates, seed=None):
    if seed is not None:
        random.seed(seed)
    
    eligible = []
    weights = []
    
    for c in candidates:
        w = calculate_weight(
            c.get('task_fit', 1.0), 
            c.get('health', 1.0), 
            c.get('remaining_fraction', 0.0), 
            c.get('hours_to_reset', 999.0), 
            c.get('promo_multiplier', 1.0)
        )
        if w > 0:
            eligible.append(c)
            weights.append(w)
            
    if not eligible:
        return None, [], []
        
    chosen = random.choices(eligible, weights=weights, k=1)[0]
    return chosen, eligible, weights
