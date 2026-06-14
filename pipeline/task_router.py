#!/usr/bin/env python3
"""
NOUS Task Router — Circuit Breaker pattern
Ruter inference-opgaver til Jetson med fallback ved OOM/fejl
"""

import os
import time
import requests
from enum import Enum

class CircuitState(Enum):
    CLOSED = "closed"      # Normal — send til Jetson
    OPEN = "open"          # Fejl — brug fallback
    HALF_OPEN = "half_open"  # Test om Jetson er tilbage

class TaskRouter:
    def __init__(self):
        self.ollama_url = os.environ.get("NOUS_OLLAMA_URL", "http://localhost:11434")
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.failure_threshold = 3      # Åbn efter 3 fejl
        self.reset_timeout = 60         # Vent 60s før half_open
        self.last_failure_time = None
        self.success_count = 0
        self.half_open_threshold = 2    # Luk igen efter 2 successer
    
    def call_ollama(self, model, prompt, stream=True):
        """Kald Ollama på Jetson med Circuit Breaker"""
        if self.state == CircuitState.OPEN:
            if time.time() - self.last_failure_time < self.reset_timeout:
                return self.fallback_response("Jetson midlertidigt utilgængelig (Circuit Breaker åben)")
            else:
                self.state = CircuitState.HALF_OPEN
                self.success_count = 0
        
        try:
            r = requests.post(
                f"{self.ollama_url}/api/generate",
                json={"model": model, "prompt": prompt, "stream": stream},
                timeout=30
            )
            r.raise_for_status()
            
            # Success
            if self.state == CircuitState.HALF_OPEN:
                self.success_count += 1
                if self.success_count >= self.half_open_threshold:
                    self.state = CircuitState.CLOSED
                    self.failure_count = 0
            else:
                self.failure_count = 0
            
            return r.json() if not stream else r
            
        except Exception as e:
            self.failure_count += 1
            if self.failure_count >= self.failure_threshold:
                self.state = CircuitState.OPEN
                self.last_failure_time = time.time()
            
            return self.fallback_response(f"Jetson fejl: {str(e)}")
    
    def embed(self, text):
        """Embedding med fallback"""
        if self.state == CircuitState.OPEN:
            return None  # Ingen embedding uden Jetson
        
        try:
            r = requests.post(
                f"{self.ollama_url}/api/embeddings",
                json={"model": "nomic-embed-text", "prompt": text},
                timeout=15
            )
            r.raise_for_status()
            return r.json()["embedding"]
        except:
            self.failure_count += 1
            return None
    
    def fallback_response(self, reason):
        """Fallback: Pi 5 CPU inference eller cached response"""
        return {
            "fallback": True,
            "reason": reason,
            "response": "Jeg kan desværre ikke behandle din forespørgsel lige nu. Prøv igen om et øjeblik.",
            "suggestion": "Hvis dette fortsætter, tjek at inference-enheden (NOUS_OLLAMA_URL) kører korrekt."
        }

# Global instance
router = TaskRouter()
