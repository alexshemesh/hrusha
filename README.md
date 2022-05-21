# hrusha
Set of fintech automations.

# Dependencies
- Python 3.8 or higher. See installation [instructions here](https://www.python.org/downloads/) 
- Use python virtual environments 
```
# Create virtual env
python -m env ~/.env/hrusha
# Activate virtual env
source ~/.env/hrusha/bin/activate
```

- install dependencies
```
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

# Configuration
Configuration is stored in ~/.hrusha/config.ini
```
[bitfinex]
API_KEY = your bitfinex key
API_SECRET = your bitfinex secret
```
# Tests
Tests are reqular pytest set. Read here [more](https://docs.pytest.org/en/7.1.x/)</br>
```
pytest .
```

