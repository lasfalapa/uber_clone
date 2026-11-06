from fastapi import FastAPI, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
import redis
import requests
import json

from database import engine, Base, get_db
import models

# Cria as tabelas no PostgreSQL automaticamente ao iniciar o app
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Uber Clone - Produção Gratuita")
templates = Jinja2Templates(directory="templates")

# Conexão com o Redis (Ex: Upstash Free Redis URL ou localhost:6379)
REDIS_URL = "redis://localhost:6379" 
# Se usar o Upstash gratuito, a URL será algo como: "redis://default:sua_senha@sua-url.upstash.io:porta"
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# Rota para o motorista atualizar sua posição GPS em tempo real (Salva no Redis)
@app.post("/api/driver/location")
async def update_driver_location(driver_id: int, lat: float, lon: float):
    redis_client.hset(f"driver:{driver_id}", mapping={"lat": lat, "lon": lon})
    return {"status": "ok", "message": "Posição atualizada no Redis"}

# Rota para buscar motoristas próximos ativos no Redis
@app.get("/api/drivers/nearby")
async def get_nearby_drivers():
    keys = redis_client.keys("driver:*")
    drivers = []
    for key in keys:
        data = redis_client.hgetall(key)
        driver_id = key.split(":")[1]
        drivers.append({
            "driver_id": int(driver_id),
            "lat": float(data["lat"]),
            "lon": float(data["lon"])
        })
    return {"drivers": drivers}

# Rota de cálculo de tarifa (Mantida do OSRM)
@app.post("/api/calculate-fare")
async def calculate_fare(data: dict):
    pickup_lat = data["pickup_lat"]
    pickup_lon = data["pickup_lon"]
    dropoff_lat = data["dropoff_lat"]
    dropoff_lon = data["dropoff_lon"]

    url = f"http://router.project-osrm.org/route/v1/driving/{pickup_lon},{pickup_lat};{dropoff_lon},{dropoff_lat}?overview=full&geometries=geojson"
    response = requests.get(url).json()
    
    if response.get("code") != "Ok":
        return {"error": "Não foi possível calcular a rota."}
    
    route = response["routes"][0]
    distance_km = route["distance"] / 1000.0
    duration_min = route["duration"] / 60.0
    
    total_fare = 4.50 + (distance_km * 1.80) + (duration_min * 0.35)
    
    return {
        "distance_km": round(distance_km, 2),
        "duration_min": round(duration_min, 1),
        "estimated_fare": round(total_fare, 2),
        "geometry": route["geometry"]
    }
from pydantic import BaseModel

class UserCreate(BaseModel):
    name: str
    email: str
    phone: str = None

@app.post("/api/users/register", status_code=201)
def register_user(user: UserCreate, db: Session = Depends(get_db)):
    existing_user = db.query(models.User).filter(models.User.email == user.email).first()
    if existing_user:
        return {"error": "Este email já está registado no sistema."}
    
    new_user = models.User(
        name=user.name,
        email=user.email,
        phone=user.phone
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    
    return {
        "status": "success",
        "message": "Utilizador registado com sucesso!",
        "user": {
            "id": new_user.id,
            "name": new_user.name,
            "email": new_user.email,
            "phone": new_user.phone
        }
    }

class DriverCreate(BaseModel):
    name: str
    vehicle_model: str = None
    plate: str = None

@app.post("/api/drivers/register", status_code=201)
def register_driver(driver: DriverCreate, db: Session = Depends(get_db)):
    new_driver = models.Driver(
        name=driver.name,
        vehicle_model=driver.vehicle_model,
        plate=driver.plate,
        is_online=0
    )
    db.add(new_driver)
    db.commit()
    db.refresh(new_driver)
    
    return {
        "status": "success",
        "message": "Motorista registado com sucesso!",
        "driver": {
            "id": new_driver.id,
            "name": new_driver.name,
            "vehicle_model": new_driver.vehicle_model,
            "plate": new_driver.plate
        }
    }

class RideCreate(BaseModel):
    user_id: int
    pickup_lat: float
    pickup_lon: float
    dropoff_lat: float
    dropoff_lon: float

@app.post("/api/rides/request", status_code=201)
def request_ride(ride_data: RideCreate, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == ride_data.user_id).first()
    if not user:
        return {"error": "Utilizador não encontrado."}
    
    url = f"http://router.project-osrm.org/route/v1/driving/{ride_data.pickup_lon},{ride_data.pickup_lat};{ride_data.dropoff_lon},{ride_data.dropoff_lat}?overview=false"
    try:
        response = requests.get(url).json()
        if response.get("code") == "Ok":
            route = response["routes"][0]
            distance_km = route["distance"] / 1000.0
            duration_min = route["duration"] / 60.0
            fare = round(4.50 + (distance_km * 1.80) + (duration_min * 0.35), 2)
        else:
            fare = 10.0
    except:
        fare = 10.0

    new_ride = models.Ride(
        user_id=ride_data.user_id,
        pickup_lat=ride_data.pickup_lat,
        pickup_lon=ride_data.pickup_lon,
        dropoff_lat=ride_data.dropoff_lat,
        dropoff_lon=ride_data.dropoff_lon,
        fare=fare,
        status="PENDING"
    )
    db.add(new_ride)
    db.commit()
    db.refresh(new_ride)
    
    return {
        "status": "success",
        "message": "Corrida solicitada com sucesso!",
        "ride": {
            "id": new_ride.id,
            "user_id": new_ride.user_id,
            "estimated_fare": new_ride.fare,
            "status": new_ride.status
        }
    }
    
class RideAccept(BaseModel):
    ride_id: int
    driver_id: int

@app.post("/api/rides/accept", status_code=200)
def accept_ride(data: RideAccept, db: Session = Depends(get_db)):
    ride = db.query(models.Ride).filter(models.Ride.id == data.ride_id).first()
    if not ride:
        return {"error": "Corrida não encontrada."}
    if ride.status != "PENDING":
        return {"error": f"Esta corrida já não está disponível (status atual: {ride.status})."}
    
    driver = db.query(models.Driver).filter(models.Driver.id == data.driver_id).first()
    if not driver:
        return {"error": "Motorista não encontrado."}
    
    ride.driver_id = data.driver_id
    ride.status = "ACCEPTED"
    db.commit()
    db.refresh(ride)
    
    return {
        "status": "success",
        "message": "Corrida aceita pelo motorista com sucesso!",
        "ride": {
            "id": ride.id,
            "user_id": ride.user_id,
            "driver_id": ride.driver_id,
            "status": ride.status,
            "estimated_fare": ride.fare
        }
    }

class UserRidesGet(BaseModel): # Ou mantenha como parâmetro na rota conforme abaixo
    pass

@app.get("/api/users/{user_id}/rides", status_code=200)
def get_user_rides(user_id: int, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        return {"error": "Utilizador não encontrado."}
    
    rides = db.query(models.Ride).filter(models.Ride.user_id == user_id).all()
    
    return {
        "status": "success",
        "user_id": user_id,
        "total_rides": len(rides),
        "rides": [
            {
                "id": r.id,
                "driver_id": r.driver_id,
                "fare": r.fare,
                "status": r.status,
                "created_at": str(r.created_at)
            } for r in rides
        ]
    }