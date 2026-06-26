import os
import time
import requests
import feedparser
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from translator import resolve_page, transliterate

def openpage(resource):
    handle = open(resource, "rb")
    data = handle.read()
    handle.close()
    return data

indexWml = openpage("documents/index.wml")
index2Wml = openpage("documents/index2.wml")
weatherWml = openpage("documents/weather.wml")
weatherErrWml = openpage("documents/weathererr.wml")
newsWml = openpage("documents/news.wml")
logoWbmp = openpage("documents/logo.wbmp")

weatherIcons = {}
for file in Path("weathericons").glob("*.wbmp"):
    weatherIcons[file.name] = openpage(os.path.join("weathericons", file.name))

weatherCache:dict[str, list] = {}
cityCache = {}
newsCache = {}

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
            "country": place["country"],
            "admin1": place.get("admin1", ""),
            "lat": place["latitude"],
            "lon": place["longitude"],
            "timezone": place["timezone"],
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
        weatherdata = r.json()["current"]

        weatherCache[city] = [time.time(), {
            "temp": weatherdata["temperature_2m"],
            "feels": weatherdata["apparent_temperature"],
            "code": weatherdata["weather_code"],
            "wind": weatherdata["wind_speed_10m"],
            "raw": data,
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
        print(f"invalid city: {city}")
        body = weatherErrWml
        ctype = "text/vnd.wap.wml"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return
    
    local_time = datetime.now(
        ZoneInfo(weather["raw"]["timezone"])
    )
    
    buffer = weatherWml.decode()
    buffer = buffer.replace("{CITYNAME}", weather["raw"]["name"].upper())
    buffer = buffer.replace("{WEATHERIMG}", getImgForCode(weather["code"]))
    buffer = buffer.replace("{WEATHERTYPE}", getDescriptionForCode(weather["code"]))
    buffer = buffer.replace("{TEMP_CELSIUS}", str(round(weather["temp"], 1)))
    buffer = buffer.replace("{TEMP_FAHRENHEIT}", str(round(weather["temp"] * 9/5 + 32, 1)))
    buffer = buffer.replace("{FEEL_CELSIUS}", str(round(weather["feels"], 1)))
    buffer = buffer.replace("{FEEL_FAHRENHEIT}", str(round(weather["feels"] * 9/5 + 32, 1)))
    buffer = buffer.replace("{WINDSPEED}", str(round(weather["wind"], 1)))
    buffer = buffer.replace("{LOCAL_TIME}", local_time.strftime("%Y-%m-%d %H:%M:%S"))
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
        newsCache[source] = [time.time() - 99999, ""]
    
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
        title:str = str(entry.get("title", "No title"))
        link:str = str(entry.get("link", ""))
        link = link.split("?")[0]
        if len(title) > 70:
            title = title[:67] + "..."
        wml += f'<a href="{link}">{transliterate(title)}</a><br/>'
        i += 1
    newsCache[source] = [time.time(), wml]
    print("Resolved news source:", source, "with headlines:", wml)
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


SELF_HOSTS = {
    "bytey",
    "bytey.local",
    "iwap.site",
    "192.168.1.5",
    "127.0.0.1",
    "localhost",
    "194.28.198.143",
    "openwave.com", # default url on UP.Browser, its not really hosting anything relevant anymore
}

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        print("GET", self.path)
    
        if self.path.startswith("/HTML2WML"):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            url = params.get("url", [""])[0]

            if not url:
                body = b"Missing URL parameter"
                ctype = "text/plain"
            elif urlparse(url).hostname in SELF_HOSTS:
                query = urlparse(url).query
                if query:
                    self.path = urlparse(url).path + "?" + query
                else:
                    self.path = urlparse(url).path
                print("Redirecting to internal path:", self.path)
            else:
                if not url.startswith("http://") and not url.startswith("https://"):
                    url = "http://" + url
                try:
                    wml = resolve_page(url)
                    body = wml.encode(encoding="utf-8")
                    ctype = "text/vnd.wap.wml"
                except Exception as e:
                    print("Error resolving page:", e)
                    body = b"Error resolving page"
                    ctype = "text/plain"
                self.send_get_response(body, ctype)
                return
        
        print("Not translating, serving static content for path:", self.path)

        if self.path == "/" or self.path == "" or self.path == "/index.wml" or self.path == "index.wml":
            body = indexWml
            ctype = "text/vnd.wap.wml"
        elif self.path == "/index2.wml" or self.path == "index2.wml":
            body = index2Wml
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
            source = params.get("source", [""])[0]
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

        self.send_get_response(body, ctype)

    def send_get_response(self, body, ctype):
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