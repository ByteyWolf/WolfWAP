import os
import time
import requests
import feedparser
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

def openpage(resource):
    handle = open(resource, "rb")
    data = handle.read()
    handle.close()
    return data

indexWml = openpage("documents/index.wml")
weatherWml = openpage("documents/weather.wml")
weatherErrWml = openpage("documents/weathererr.wml")
newsWml = openpage("documents/news.wml")
logoWbmp = openpage("documents/logo.wbmp")

weatherIcons = {}
for file in Path("weathericons").glob("*.wbmp"):
    weatherIcons[file.name] = openpage(os.path.join("weathericons", file.name))

weatherCache:dict[str, list] = {}
cityCache = {}
newsCache = {
    "bbc": [],
    "guard": [],
    "ukr": [],
}

def geocode_city(city):
    city = city.strip().lower()
    if not city in cityCache:
        r = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1},
            timeout=10,
        )

        r.raise_for_status()

        data = r.json()

        if "results" not in data:
            cityCache[city] = False
            return False

        if len(data["results"]) == 0:
            cityCache[city] = False
            return False

        place = data["results"][0]
        cityCache[city] = {
            "name": place["name"],
            "lat": place["latitude"],
            "lon": place["longitude"],
        }

    return cityCache[city]

def getWeather(city):
    city = city.strip().lower()
    data = geocode_city(city)
    if not data:
        return False
    if not city in weatherCache or time.time() - weatherCache[city][0] > 30:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": data["lat"],
                "longitude": data["lon"],
                "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
                "timezone": "auto",
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()["current"]

        weatherCache[city] = [time.time(), {
            "temp": data["temperature_2m"],
            "feels": data["apparent_temperature"],
            "code": data["weather_code"],
            "wind": data["wind_speed_10m"],

        }]
    return weatherCache[city][1]

def getImgForCode(code):
    # we have cloudy rainy snowy sunny
    if code == 0:
        return "sunny.wbmp"
    elif code in [1, 2, 3]:
        return "cloudy.wbmp"
    elif code in [45, 48, 51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82]:
        return "rainy.wbmp"
    elif code in [71, 73, 75, 77, 85, 86]:
        return "snowy.wbmp"
    else:
        return "cloudy.wbmp"
    
def getDescriptionForCode(code):
    descriptions = {
        0: "Sunny",
        1: "Mainly Sunny",
        2: "Partly Cloudy",
        3: "Overcast",
        45: "Fog",
        48: "Depositing Rime Fog",
        51: "Light Drizzle",
        53: "Moderate Drizzle",
        55: "Dense Drizzle",
        56: "Light Freezing Drizzle",
        57: "Dense Freezing Drizzle",
        61: "Light Rain",
        63: "Moderate Rain",
        65: "Dense Rain",
        66: "Light Freezing Rain",
        67: "Dense Freezing Rain",
        80: "Slight Rain showers",
        81: "Moderate Rain showers",
        82: "Violent Rain showers",
        71: "Slight Snowfall",
        73: "Moderate Snowfall",
        75: "Dense Snowfall",
        77: "Snow Grains",
        85: "Slight Snow showers",
        86: "Violent Snow showers"
    }
    return descriptions.get(code, "Unknown")

def doweather(self, city):
    weather = getWeather(city)
    print(weather)
    if not weather:
        print("invalid city")
        body = weatherErrWml
        ctype = "text/vnd.wap.wml"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return
    
    buffer = weatherWml.decode()
    buffer = buffer.replace("{CITYNAME}", city.upper())
    buffer = buffer.replace("{WEATHERIMG}", getImgForCode(weather["code"]))
    buffer = buffer.replace("{WEATHERTYPE}", getDescriptionForCode(weather["code"]))
    buffer = buffer.replace("{TEMP_CELSIUS}", str(round(weather["temp"], 1)))
    buffer = buffer.replace("{TEMP_FAHRENHEIT}", str(round(weather["temp"] * 9/5 + 32, 1)))
    buffer = buffer.replace("{FEEL_CELSIUS}", str(round(weather["feels"], 1)))
    buffer = buffer.replace("{FEEL_FAHRENHEIT}", str(round(weather["feels"] * 9/5 + 32, 1)))
    buffer = buffer.replace("{WINDSPEED}", str(round(weather["wind"], 1)))
    body = buffer.encode(encoding="utf-8")
    ctype = "text/vnd.wap.wml"

    print("yay")
    self.send_response(200)
    self.send_header("Content-Type", ctype)
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()

    self.wfile.write(body)
    print(body)

def resolve_news_source(source):
    if not source in newsCache:
        newsCache[source] = [time.time() + 99999, ""]
    
    if time.time() - newsCache[source][0] < 60:
        return newsCache[source][1]

    rsslink = None
    if source == "bbc":
        rsslink = "http://feeds.bbci.co.uk/news/world/rss.xml"
    elif source == "guard":
        rsslink = "https://www.theguardian.com/world/rss"
    elif source == "ukr":
        rsslink = "https://www.pravda.com.ua/rss/view_news/"
    if not rsslink:
        return "Invalid source"
    
    feed = feedparser.parse(rsslink)
    if feed.bozo:
        print("RSS parse warning:", feed.bozo_exception)

    wml = ""

    i = 0
    for entry in feed.entries[:5]:
        wml += f'<a href="/newsview.wml?s={source}&h={i}">{entry.get("title", "No title")}</a><br/>'
        i += 1
    newsCache[source] = [time.time(), wml]
    return wml

def donews(self, source):
    wml = resolve_news_source(source)
    if wml == "Invalid source":
        body = b"Invalid source"
        ctype = "text/plain"
        self.send_response(400)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return

    buffer = newsWml.decode()
    buffer = buffer.replace("{SOURCE}", source.upper())
    buffer = buffer.replace("{HEADLINES}", wml)
    body = buffer.encode(encoding="utf-8")
    ctype = "text/vnd.wap.wml"

    self.send_response(200)
    self.send_header("Content-Type", ctype)
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()

    self.wfile.write(body)



class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            body = indexWml
            ctype = "text/vnd.wap.wml"

        elif self.path == "/logo.wbmp":
            body = logoWbmp
            ctype = "image/vnd.wap.wbmp"

        elif self.path.startswith("/weather.wml"):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            city = params.get("c", [""])[0]
            return doweather(self, city)
        
        elif self.path.startswith("/news.wml"):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            source = params.get("s", [""])[0]
            return donews(self, source)

        elif self.path.startswith("/ic/"):
            icontype = self.path.replace("/ic/", "")
            if icontype in weatherIcons:
                body = weatherIcons[icontype]
                ctype = "image/vnd.wap.wbmp"
            else:
                body = b"404"
                ctype = "text/plain"

        else:
            body = b"404"
            ctype = "text/plain"

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

        self.wfile.write(body)

    def do_POST(self):
        if self.path.startswith("/weather.wml"):
            length = int(self.headers["Content-Length"])
            body = self.rfile.read(length)

            data = parse_qs(body.decode())

            city = data.get("c", [""])[0]
            return doweather(self, city)
        elif self.path.startswith("/news.wml"):
            length = int(self.headers["Content-Length"])
            body = self.rfile.read(length)

            data = parse_qs(body.decode())

            source = data.get("s", [""])[0]
            return donews(self, source)
        body = b"Invalid request type"
        ctype = "text/plain"
        self.send_response(400)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return

        


HTTPServer(("0.0.0.0", 80), Handler).serve_forever()