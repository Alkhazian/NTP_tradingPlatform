
import math

class BlackScholes:
    """
    Simple Black-Scholes calculator for European options.
    Uses pure Python (no scipy/numpy) for portability.
    """

    @staticmethod
    def norm_pdf(x):
        """Standard normal probability density function"""
        return math.exp(-0.5 * x**2) / math.sqrt(2 * math.pi)

    @staticmethod
    def norm_cdf(x):
        """
        Cumulative distribution function for the standard normal distribution.
        Uses approximation from Abramowitz and Stegun 7.1.26.
        Accuracy is better than 1e-7.
        """
        p = 0.3275911
        a1 = 0.254829592
        a2 = -0.284496736
        a3 = 1.421413741
        a4 = -1.453152027
        a5 = 1.061405429
        
        sign = 1
        if x < 0:
            sign = -1
        x = abs(x) / math.sqrt(2.0)
        
        t = 1.0 / (1.0 + p * x)
        y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x * x)
        
        return 0.5 * (1.0 + sign * y)

    @staticmethod
    def d1_d2(S, K, T, r, sigma, q=0.0):
        """Calculate d1 and d2 parameters with dividend yield q"""
        if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
            return 0.0, 0.0
            
        d1 = (math.log(S / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        return d1, d2

    @staticmethod
    def price(option_type, S, K, T, r, sigma, q=0.0):
        """
        Calculate option price with dividend yield q.
        """
        if T <= 0:
            # At expiry, price is intrinsic value
            if option_type.lower().startswith('c'):
                return max(0.0, S - K)
            else:
                return max(0.0, K - S)
                
        d1, d2 = BlackScholes.d1_d2(S, K, T, r, sigma, q)
        
        if option_type.lower().startswith('c'):
            return S * math.exp(-q * T) * BlackScholes.norm_cdf(d1) - K * math.exp(-r * T) * BlackScholes.norm_cdf(d2)
        else:
            return K * math.exp(-r * T) * BlackScholes.norm_cdf(-d2) - S * math.exp(-q * T) * BlackScholes.norm_cdf(-d1)

    @staticmethod
    def delta(option_type, S, K, T, r, sigma, q=0.0):
        """Calculate Delta (with dividend adjustment e^-qT)"""
        if T <= 0:
            if option_type.lower().startswith('c'):
                return 1.0 if S > K else 0.0
            else:
                return -1.0 if S < K else 0.0
                
        d1, _ = BlackScholes.d1_d2(S, K, T, r, sigma, q)
        
        if option_type.lower().startswith('c'):
            return math.exp(-q * T) * BlackScholes.norm_cdf(d1)
        else:
            return math.exp(-q * T) * (BlackScholes.norm_cdf(d1) - 1.0)

    @staticmethod
    def vega(S, K, T, r, sigma, q=0.0):
        """Calculate Vega"""
        if T <= 0:
            return 0.0
        d1, _ = BlackScholes.d1_d2(S, K, T, r, sigma, q)
        return S * math.exp(-q * T) * BlackScholes.norm_pdf(d1) * math.sqrt(T)

    @staticmethod
    def implied_volatility(target_price, option_type, S, K, T, r, q=0.0, initial_guess=0.5):
        """
        Calculate Implied Volatility using Newton-Raphson.
        """
        MAX_ITERATIONS = 20
        PRECISION = 1e-4
        
        sigma = initial_guess
        
        for i in range(MAX_ITERATIONS):
            price = BlackScholes.price(option_type, S, K, T, r, sigma, q)
            vega = BlackScholes.vega(S, K, T, r, sigma, q)
            
            diff = target_price - price
            
            if abs(diff) < PRECISION:
                return sigma
                
            if abs(vega) < 1e-8:
                return sigma # Failsafe
                
            sigma = sigma + diff / vega  # Newton-Raphson step
            
            # Clamp sigma to reasonable bounds
            sigma = max(0.01, min(5.0, sigma))
            
        return sigma

