import time
import redis

class RateLimitTimeoutError(Exception):
    """Raised when token wait exceeds timeout. Celery task should self.retry(countdown=30)."""
    pass

class RateLimiter:
    """Redis-based distributed token bucket rate limiter.
    
    Prevents LLM API rate limit violations with configurable RPM/TPM.
    Uses wait_and_acquire() for blocking wait with timeout (v3 anti-avalanche).
    """
    
    def __init__(self, redis_client: redis.Redis, key: str,
                 rpm: int = 500, tpm: int = 150000):
        self.redis = redis_client
        self.key = key
        self.rpm = rpm
        self.tpm = tpm
        # Token bucket keys
        self._rpm_key = f"ratelimit:{key}:rpm"
        self._tpm_key = f"ratelimit:{key}:tpm"
    
    def acquire(self, estimated_tokens: int = 1000) -> bool:
        """Non-blocking attempt to acquire tokens.
        Returns True if acquired, False if rate limited.
        """
        now = time.time()
        minute_ago = now - 60
        
        # Clean old entries
        pipe = self.redis.pipeline()
        pipe.zremrangebyscore(self._rpm_key, "-inf", minute_ago)
        pipe.zremrangebyscore(self._tpm_key, "-inf", minute_ago)
        pipe.execute()
        
        # Check current counts
        rpm_count = self.redis.zcard(self._rpm_key)
        
        # TPM: sum tokens from all entries in the 60s window
        # Each entry is stored as member="timestamp:tokens" with score=timestamp
        tpm_entries = self.redis.zrangebyscore(self._tpm_key, minute_ago, "+inf")
        tpm_count = sum(int(entry.split(":")[1]) for entry in tpm_entries) if tpm_entries else 0
        
        if rpm_count >= self.rpm or tpm_count + estimated_tokens > self.tpm:
            return False
        
        # Acquire tokens
        pipe = self.redis.pipeline()
        pipe.zadd(self._rpm_key, {f"{now}": now})
        pipe.zadd(self._tpm_key, {f"{now}:{estimated_tokens}": now})
        pipe.expire(self._rpm_key, 120)
        pipe.expire(self._tpm_key, 120)
        pipe.execute()
        
        return True
    
    def wait_and_acquire(self, estimated_tokens: int = 1000,
                         timeout: float = 10.0) -> None:
        """v3: Blocking wait for tokens, max timeout seconds.
        
        Raises RateLimitTimeoutError if timeout exceeded.
        NEVER use infinite sleep - that causes worker starvation.
        """
        deadline = time.time() + timeout
        backoff = 0.1  # Start with 100ms backoff
        
        while time.time() < deadline:
            if self.acquire(estimated_tokens):
                return
            
            # Exponential backoff with cap
            sleep_time = min(backoff, deadline - time.time())
            if sleep_time <= 0:
                break
            time.sleep(sleep_time)
            backoff = min(backoff * 2, 1.0)  # Cap at 1 second
        
        raise RateLimitTimeoutError(
            f"Rate limit timeout after {timeout}s for {self.key}"
        )
