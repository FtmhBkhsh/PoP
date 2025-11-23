import os 
import json 
import socket


class Config:
    def __init__(self, config_file="Config\config.json"):
        node_ip = socket.gethostbyname(socket.gethostname())
        self.config_file = config_file
        self.default_config = {
            "challenge_producer": {
                "challenges_count": 1,
                "challenges_count": 1,
                "challenges_file_path": "challenges.csv",
                "challenge_producer_ip": node_ip,
                "port": 5000
            },
            "server": {
                "host": "0.0.0.0",
                "port": 5000,
                "debug": True,
                "workers": 4
            },
            "logging": {
                "level": "INFO",
                "file": "app.log",
                "max_size": "10MB"
            }
        }
        self.config = self.load_config()
    
    def load_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    user_config = json.load(f)
                    # Deep merge with defaults
                    self.merge_dicts(self.default_config, user_config)
                    print(f"✅ Configuration loaded from {self.config_file}")
            except Exception as e:
                print(f"❌ Error loading config: {e}. Using defaults.")
        else:
            self.create_default_config()
        
        return self.default_config
    
    def merge_dicts(self, default, user):
        for key, value in user.items():
            if key in default and isinstance(default[key], dict) and isinstance(value, dict):
                self.merge_dicts(default[key], value)
            else:
                default[key] = value
    
    def create_default_config(self):
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.default_config, f, indent=4, ensure_ascii=False)
            print(f"📁 Default config created: {self.config_file}")
        except Exception as e:
            print(f"❌ Error creating config: {e}")
    
    def get(self, section: str, key: str, default=None):
        section_data = self.config.get(section, {})
        if isinstance(section_data, dict):
            return section_data.get(key, default)
        return default