#!/usr/bin/env python3
"""
Ollama Local LLM Integration for Claude Code TTS System
======================================================

Core interface for managing Ollama local LLM processing with automatic
installation, model management, and intelligent resource optimization.

Features:
- Automatic Ollama installation and setup
- Dynamic model selection based on system resources
- Model downloading with progress tracking
- Health monitoring and connection management
- Performance optimization and resource management
- Integration with existing TTS system architecture

Author: Claude Code AI Assistant
Created: 2025-01-13
"""

import os
import sys
import json
import time
import psutil
import requests
import subprocess
import threading
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple, Union
from dataclasses import dataclass, asdict
from contextlib import contextmanager
import hashlib
import platform


# Configuration Constants
def _ollama_api_base() -> str:
    """Resolve the Ollama endpoint, honoring the standard ``OLLAMA_HOST`` env
    var (the same one the Ollama CLI/server use). Accepts a full URL
    (``http://host:port``) or a bare ``host[:port]`` (scheme assumed http).
    Unset → local default. Read at import; set it before the daemon starts."""
    raw = os.environ.get("OLLAMA_HOST", "").strip()
    if not raw:
        return "http://localhost:11434"
    if "://" not in raw:
        raw = "http://" + raw
    return raw.rstrip("/")


OLLAMA_API_BASE = _ollama_api_base()
OLLAMA_INSTALL_PATH = Path.home() / ".ollama"
OLLAMA_CONFIG_PATH = Path.home() / ".claude" / "ollama_config.json"
OLLAMA_METRICS_PATH = Path.home() / ".claude" / "ollama_metrics.json"
OLLAMA_LOG_PATH = Path.home() / ".claude" / "logs" / "ollama.log"

# Performance Constants
DEFAULT_TIMEOUT = 30
MAX_RETRIES = 3
HEALTH_CHECK_INTERVAL = 60
METRICS_UPDATE_INTERVAL = 30
MODEL_LOAD_TIMEOUT = 300
RESPONSE_TIMEOUT = 180


@dataclass
class ModelInfo:
    """Information about an available Ollama model"""
    name: str
    size_gb: float
    ram_required_gb: float
    parameters: str
    description: str
    quantization: str = "fp16"
    performance_tier: str = "standard"

    @property
    def short_name(self) -> str:
        """Get short name without tag"""
        return self.name.split(':')[0]


@dataclass
class SystemResources:
    """System resource information"""
    total_ram_gb: float
    available_ram_gb: float
    cpu_cores: int
    gpu_available: bool = False
    gpu_memory_gb: float = 0.0
    platform: str = ""

    @classmethod
    def detect(cls) -> 'SystemResources':
        """Detect current system resources"""
        memory = psutil.virtual_memory()
        return cls(
            total_ram_gb=memory.total / (1024**3),
            available_ram_gb=memory.available / (1024**3),
            cpu_cores=psutil.cpu_count(logical=False),
            gpu_available=cls._detect_gpu(),
            platform=platform.system().lower()
        )

    @staticmethod
    def _detect_gpu() -> bool:
        """Detect GPU availability (basic check)"""
        try:
            # Check for NVIDIA GPU on macOS/Linux
            result = subprocess.run(['nvidia-smi'],
                                  capture_output=True, text=True, timeout=5)
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        try:
            # Check for Apple Silicon GPU
            result = subprocess.run(['system_profiler', 'SPDisplaysDataType'],
                                  capture_output=True, text=True, timeout=5)
            return 'Apple' in result.stdout and 'GPU' in result.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        return False


class OllamaInstaller:
    """Handles Ollama installation and setup"""

    def __init__(self):
        self.install_path = OLLAMA_INSTALL_PATH
        self.logger = logging.getLogger(__name__)

    def is_installed(self) -> bool:
        """Check if Ollama is installed and accessible"""
        try:
            result = subprocess.run(['ollama', '--version'],
                                  capture_output=True, text=True, timeout=10)
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def install(self) -> bool:
        """Install Ollama automatically"""
        if self.is_installed():
            self.logger.info("Ollama already installed")
            return True

        try:
            system = platform.system().lower()
            if system == "darwin":  # macOS
                return self._install_macos()
            elif system == "linux":
                return self._install_linux()
            else:
                self.logger.error(f"Unsupported platform: {system}")
                return False

        except Exception as e:
            self.logger.error(f"Installation failed: {e}")
            return False

    def _install_macos(self) -> bool:
        """Install Ollama on macOS"""
        try:
            # Download and run the official installer
            self.logger.info("Downloading Ollama installer for macOS...")

            install_script = """
            curl -fsSL https://ollama.com/install.sh | sh
            """

            result = subprocess.run(install_script, shell=True,
                                  capture_output=True, text=True, timeout=300)

            if result.returncode == 0:
                self.logger.info("Ollama installed successfully")
                return self._verify_installation()
            else:
                self.logger.error(f"Installation failed: {result.stderr}")
                return False

        except subprocess.TimeoutExpired:
            self.logger.error("Installation timed out")
            return False

    def _install_linux(self) -> bool:
        """Install Ollama on Linux"""
        try:
            self.logger.info("Installing Ollama on Linux...")

            install_script = """
            curl -fsSL https://ollama.com/install.sh | sh
            """

            result = subprocess.run(install_script, shell=True,
                                  capture_output=True, text=True, timeout=300)

            if result.returncode == 0:
                self.logger.info("Ollama installed successfully")
                return self._verify_installation()
            else:
                self.logger.error(f"Installation failed: {result.stderr}")
                return False

        except subprocess.TimeoutExpired:
            self.logger.error("Installation timed out")
            return False

    def _verify_installation(self) -> bool:
        """Verify Ollama installation"""
        time.sleep(2)  # Allow installation to complete
        return self.is_installed()


class OllamaModelManager:
    """Manages Ollama model downloading and selection"""

    AVAILABLE_MODELS = [
        ModelInfo("llama2:7b-chat", 3.8, 8.0, "7B", "Llama 2 7B Chat optimized"),
        ModelInfo("llama2:13b-chat", 7.3, 16.0, "13B", "Llama 2 13B Chat model"),
        ModelInfo("llama2:7b-chat-q4_0", 2.0, 4.0, "7B", "Llama 2 7B quantized", "q4_0", "fast"),
        ModelInfo("mistral:7b", 4.1, 8.0, "7B", "Mistral 7B Instruct"),
        ModelInfo("codellama:7b", 3.8, 8.0, "7B", "Code Llama 7B for coding"),
        ModelInfo("llama3:8b", 4.7, 8.0, "8B", "Llama 3 8B Instruct"),
        ModelInfo("phi3:mini", 2.3, 4.0, "3.8B", "Phi-3 Mini efficient model", "fp16", "fast"),
    ]

    def __init__(self, api_base: str = OLLAMA_API_BASE):
        self.api_base = api_base
        self.logger = logging.getLogger(__name__)

    def list_downloaded_models(self) -> List[str]:
        """List models that are downloaded and available"""
        try:
            response = requests.get(f"{self.api_base}/api/tags", timeout=DEFAULT_TIMEOUT)
            if response.status_code == 200:
                models_data = response.json()
                return [model['name'] for model in models_data.get('models', [])]
            return []
        except Exception as e:
            self.logger.debug(f"Failed to list models: {e}")
            return []

    def select_optimal_model(self, resources: SystemResources,
                           complexity: str = "medium") -> Optional[ModelInfo]:
        """Select optimal model based on system resources and query complexity"""
        available_models = [m for m in self.AVAILABLE_MODELS
                          if m.ram_required_gb <= resources.available_ram_gb]

        if not available_models:
            # Fallback to smallest model if nothing fits
            return min(self.AVAILABLE_MODELS, key=lambda m: m.ram_required_gb)

        # Sort by performance preference
        if complexity == "high" and resources.available_ram_gb >= 16:
            # Prefer larger models for complex queries
            preferred = [m for m in available_models if "13b" in m.name.lower()]
            if preferred:
                return preferred[0]
        elif complexity == "low" or resources.available_ram_gb < 8:
            # Prefer fast/quantized models for simple queries or limited resources
            preferred = [m for m in available_models if m.performance_tier == "fast"]
            if preferred:
                return preferred[0]

        # Default: best model that fits in available RAM
        return max(available_models, key=lambda m: m.size_gb)

    def download_model(self, model_name: str, progress_callback=None) -> bool:
        """Download a model with progress tracking"""
        try:
            self.logger.info(f"Starting download of model: {model_name}")

            # Start the download
            response = requests.post(f"{self.api_base}/api/pull",
                                   json={"name": model_name},
                                   stream=True, timeout=MODEL_LOAD_TIMEOUT)

            if response.status_code != 200:
                self.logger.error(f"Failed to start model download: {response.status_code}")
                return False

            # Track download progress
            for line in response.iter_lines():
                if line:
                    try:
                        data = json.loads(line.decode('utf-8'))
                        if progress_callback and 'status' in data:
                            progress_callback(data)

                        if data.get('status') == 'success':
                            self.logger.info(f"Model {model_name} downloaded successfully")
                            return True

                    except json.JSONDecodeError:
                        continue

            return False

        except Exception as e:
            self.logger.error(f"Model download failed: {e}")
            return False

    def ensure_model_available(self, model_name: str) -> bool:
        """Ensure a model is downloaded and available"""
        downloaded_models = self.list_downloaded_models()

        if model_name in downloaded_models:
            return True

        self.logger.info(f"Model {model_name} not found, downloading...")

        def progress_callback(data):
            status = data.get('status', '')
            if 'completed' in data and 'total' in data:
                progress = (data['completed'] / data['total']) * 100
                self.logger.info(f"Download progress: {progress:.1f}% - {status}")
            else:
                self.logger.info(f"Download status: {status}")

        return self.download_model(model_name, progress_callback)


class OllamaClient:
    """Main client for interacting with Ollama API"""

    def __init__(self, api_base: str = OLLAMA_API_BASE):
        self.api_base = api_base
        self.logger = logging.getLogger(__name__)
        self.installer = OllamaInstaller()
        self.model_manager = OllamaModelManager(api_base)

        # State tracking
        self.current_model = None
        self.model_loaded = False
        self.last_health_check = 0
        self.health_status = "unknown"

        # Performance metrics
        self.metrics = {
            "requests_total": 0,
            "requests_successful": 0,
            "avg_response_time": 0.0,
            "model_load_time": 0.0,
            "last_request_time": 0
        }

    def initialize(self) -> bool:
        """Initialize Ollama client with installation if needed"""
        try:
            # Check if Ollama is installed
            if not self.installer.is_installed():
                self.logger.info("Ollama not found, installing...")
                if not self.installer.install():
                    self.logger.error("Failed to install Ollama")
                    return False

            # Start Ollama service if not running
            if not self.is_service_running():
                self.logger.info("Starting Ollama service...")
                if not self.start_service():
                    self.logger.error("Failed to start Ollama service")
                    return False

            # Wait for service to be ready
            if not self.wait_for_service(timeout=30):
                self.logger.error("Ollama service failed to start")
                return False

            self.logger.info("Ollama client initialized successfully")
            return True

        except Exception as e:
            self.logger.error(f"Initialization failed: {e}")
            return False

    def is_service_running(self) -> bool:
        """Check if Ollama service is running"""
        try:
            response = requests.get(f"{self.api_base}/api/tags", timeout=5)
            return response.status_code == 200
        except Exception:
            return False

    def start_service(self) -> bool:
        """Start Ollama service"""
        try:
            # Start Ollama in background
            subprocess.Popen(['ollama', 'serve'],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            return True
        except Exception as e:
            self.logger.error(f"Failed to start service: {e}")
            return False

    def wait_for_service(self, timeout: int = 30) -> bool:
        """Wait for Ollama service to be ready"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            if self.is_service_running():
                return True
            time.sleep(1)
        return False

    def load_model(self, model_name: str) -> bool:
        """Load a specific model"""
        try:
            # Ensure model is available
            if not self.model_manager.ensure_model_available(model_name):
                return False

            # Load the model
            self.logger.info(f"Loading model: {model_name}")
            start_time = time.time()

            response = requests.post(f"{self.api_base}/api/generate",
                                   json={
                                       "model": model_name,
                                       "prompt": "",
                                       "stream": False
                                   },
                                   timeout=MODEL_LOAD_TIMEOUT)

            if response.status_code == 200:
                load_time = time.time() - start_time
                self.metrics["model_load_time"] = load_time
                self.current_model = model_name
                self.model_loaded = True
                self.logger.info(f"Model loaded successfully in {load_time:.2f}s")
                return True
            else:
                self.logger.error(f"Failed to load model: {response.status_code}")
                return False

        except Exception as e:
            self.logger.error(f"Model loading failed: {e}")
            return False

    def generate_response(self, prompt: str, model: Optional[str] = None,
                         max_tokens: int = 500, temperature: float = 0.7,
                         keep_alive: Optional[object] = None) -> Optional[str]:
        """Generate response using Ollama.

        keep_alive (optional): forwarded as the top-level /api/generate
        ``keep_alive`` field — e.g. "30m" to hold the model in memory for 30
        minutes, or -1 to pin it indefinitely. Keeps `model` warm so the first
        request after an idle gap doesn't pay cold-load latency (R2).
        """
        try:
            start_time = time.time()
            self.metrics["requests_total"] += 1

            # Use current model if none specified
            if not model:
                model = self.current_model

            if not model:
                # Auto-select model based on system resources
                resources = SystemResources.detect()
                optimal_model = self.model_manager.select_optimal_model(resources)
                if not optimal_model:
                    self.logger.error("No suitable model found")
                    return None
                model = optimal_model.name

                # Load the model if not already loaded
                if model != self.current_model:
                    if not self.load_model(model):
                        return None

            # Generate response
            payload = {
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": temperature,
                    "num_predict": max_tokens,
                },
            }
            if keep_alive is not None:
                # Top-level field (NOT inside "options"). Keeps the model warm.
                payload["keep_alive"] = keep_alive
            response = requests.post(f"{self.api_base}/api/generate",
                                   json=payload,
                                   timeout=RESPONSE_TIMEOUT)

            if response.status_code == 200:
                data = response.json()
                generated_text = data.get('response', '').strip()

                # Update metrics
                response_time = time.time() - start_time
                self.metrics["requests_successful"] += 1
                self.metrics["avg_response_time"] = (
                    (self.metrics["avg_response_time"] * (self.metrics["requests_successful"] - 1) + response_time)
                    / self.metrics["requests_successful"]
                )
                self.metrics["last_request_time"] = time.time()

                self.logger.info(f"Generated response in {response_time:.2f}s")
                return generated_text
            else:
                self.logger.error(f"Generation failed: {response.status_code}")
                return None

        except Exception as e:
            self.logger.error(f"Response generation failed: {e}")
            return None

    def health_check(self) -> Dict[str, Any]:
        """Perform health check and return status"""
        try:
            start_time = time.time()

            # Check service availability
            response = requests.get(f"{self.api_base}/api/tags", timeout=10)
            service_healthy = response.status_code == 200

            # Check response time
            response_time = time.time() - start_time

            # Get system resources
            resources = SystemResources.detect()

            health_data = {
                "timestamp": time.time(),
                "service_running": service_healthy,
                "response_time": response_time,
                "current_model": self.current_model,
                "model_loaded": self.model_loaded,
                "system_resources": asdict(resources),
                "metrics": self.metrics.copy(),
                "status": "healthy" if service_healthy and response_time < 5.0 else "degraded"
            }

            self.health_status = health_data["status"]
            self.last_health_check = time.time()

            return health_data

        except Exception as e:
            self.logger.error(f"Health check failed: {e}")
            return {
                "timestamp": time.time(),
                "status": "unhealthy",
                "error": str(e)
            }

    def get_model_info(self) -> Dict[str, Any]:
        """Get information about current model and available models"""
        try:
            downloaded_models = self.model_manager.list_downloaded_models()
            available_models = [asdict(m) for m in self.model_manager.AVAILABLE_MODELS]

            return {
                "current_model": self.current_model,
                "model_loaded": self.model_loaded,
                "downloaded_models": downloaded_models,
                "available_models": available_models,
                "recommended_model": self._get_recommended_model()
            }

        except Exception as e:
            self.logger.error(f"Failed to get model info: {e}")
            return {}

    def _get_recommended_model(self) -> Optional[str]:
        """Get recommended model for current system"""
        try:
            resources = SystemResources.detect()
            optimal_model = self.model_manager.select_optimal_model(resources)
            return optimal_model.name if optimal_model else None
        except Exception:
            return None

    def cleanup(self):
        """Cleanup resources"""
        self.logger.info("Cleaning up Ollama client")
        # Any cleanup needed


def main():
    """Test and demonstration of Ollama integration"""
    import argparse

    parser = argparse.ArgumentParser(description="Ollama Integration for Claude Code TTS")
    parser.add_argument("--install", action="store_true", help="Install Ollama")
    parser.add_argument("--test", action="store_true", help="Test Ollama integration")
    parser.add_argument("--health", action="store_true", help="Check health status")
    parser.add_argument("--models", action="store_true", help="List model information")
    parser.add_argument("--chat", action="store_true", help="Interactive chat mode")
    parser.add_argument("--model", type=str, help="Specific model to use")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")

    args = parser.parse_args()

    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    client = OllamaClient()

    try:
        if args.install:
            print("Installing Ollama...")
            success = client.installer.install()
            print(f"Installation {'successful' if success else 'failed'}")
            return

        if args.health:
            print("Checking Ollama health...")
            health = client.health_check()
            print(json.dumps(health, indent=2))
            return

        if args.models:
            print("Model information:")
            info = client.get_model_info()
            print(json.dumps(info, indent=2))
            return

        if args.test:
            print("Testing Ollama integration...")

            # Initialize client
            if not client.initialize():
                print("Failed to initialize Ollama")
                return

            # Test response generation
            test_prompt = "Hello! Can you help me with Python programming?"
            print(f"Test prompt: {test_prompt}")

            response = client.generate_response(test_prompt, model=args.model)
            if response:
                print(f"Response: {response}")
            else:
                print("Failed to generate response")

            return

        if args.chat:
            print("Starting interactive chat mode...")
            print("Type 'quit' to exit")

            if not client.initialize():
                print("Failed to initialize Ollama")
                return

            while True:
                try:
                    user_input = input("\nYou: ").strip()
                    if user_input.lower() in ['quit', 'exit', 'bye']:
                        break

                    if user_input:
                        response = client.generate_response(user_input, model=args.model)
                        if response:
                            print(f"Assistant: {response}")
                        else:
                            print("Sorry, I couldn't generate a response.")

                except KeyboardInterrupt:
                    print("\nGoodbye!")
                    break

            return

        # Default: show usage
        parser.print_help()

    except Exception as e:
        logging.error(f"Error: {e}")
    finally:
        client.cleanup()


if __name__ == "__main__":
    main()