"""GeoLog - demonstracao de persistencia poliglota com Streamlit."""

from __future__ import annotations

import os
import sqlite3
import random
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
import folium
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from streamlit_folium import st_folium


APP_DIR = Path(__file__).resolve().parent
SQLITE_PATH = APP_DIR / "logitech.db"
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
MONGO_DB = "geolog_db"
COLLECTION = "telemetria"

MOTORISTAS = [
    (1, "Carlos Andrade", "123456789", "Ativo"),
    (2, "Mariana Silva", "987654321", "Ativo"),
    (3, "Roberto Souza", "456789123", "Em Descanso"),
]
VEICULOS = [
    (101, "ABC-1A23", "Volvo FH 540", 1),
    (102, "XYZ-9876", "Scania R450", 2),
    (103, "KGB-4567", "Mercedes Actros", 3),
]
TELEMETRIA_SEED = [
    {"veiculo_id": 101, "location": {"type": "Point", "coordinates": [-34.873, -7.115]}, "temperatura": 4.2, "velocidade": 65, "timestamp": "2026-09-11T10:00:00Z"},
    {"veiculo_id": 102, "location": {"type": "Point", "coordinates": [-34.832, -7.121]}, "temperatura": -18.5, "velocidade": 85, "timestamp": "2026-09-11T10:05:00Z"},
    {"veiculo_id": 103, "location": {"type": "Point", "coordinates": [-34.950, -7.150]}, "temperatura": 22.0, "velocidade": 0, "timestamp": "2026-09-11T09:45:00Z"},
]
PONTOS_APOIO = {
    "Joao Pessoa - Centro": (-7.115, -34.873),
    "Joao Pessoa - Cabo Branco": (-7.121, -34.832),
    "Joao Pessoa - Tibiri / BR-230": (-7.150, -34.950),
}


def utc_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def abrir_sqlite() -> sqlite3.Connection:
    con = sqlite3.connect(SQLITE_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS motoristas (
            id INTEGER PRIMARY KEY, nome TEXT NOT NULL, cnh TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS veiculos (
            id INTEGER PRIMARY KEY, placa TEXT NOT NULL UNIQUE, modelo TEXT NOT NULL,
            motorista_id INTEGER NOT NULL REFERENCES motoristas(id)
        );
    """)
    con.executemany("INSERT OR IGNORE INTO motoristas VALUES (?, ?, ?, ?)", MOTORISTAS)
    con.executemany("INSERT OR IGNORE INTO veiculos VALUES (?, ?, ?, ?)", VEICULOS)
    con.commit()
    return con


def conectar_mongodb():
    client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=2500)
    client.admin.command("ping")
    collection = client[MONGO_DB][COLLECTION]
    # Indice geoespacial obrigatorio, criado durante a inicializacao.
    collection.create_index([("location", "2dsphere")], name="location_2dsphere")
    for item in TELEMETRIA_SEED:
        timestamp = utc_datetime(item["timestamp"])
        collection.update_one(
            {"veiculo_id": item["veiculo_id"], "timestamp": timestamp},
            {"$setOnInsert": {**item, "timestamp": timestamp}},
            upsert=True,
        )
    return client, collection


def ultimas_leituras(collection) -> dict[int, dict]:
    pipeline = [
        {"$sort": {"timestamp": -1}},
        {"$group": {"_id": "$veiculo_id", "registro": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$registro"}},
    ]
    return {doc["veiculo_id"]: doc for doc in collection.aggregate(pipeline)}


def distancia_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import asin, cos, radians, sin, sqrt
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 6371.0088 * 2 * asin(sqrt(a))


def dataframe_unificado(con, latest: dict[int, dict]) -> pd.DataFrame:
    cadastro = pd.read_sql_query("""
        SELECT m.nome AS motorista, m.status AS status_motorista,
               v.id AS veiculo_id, v.placa, v.modelo
        FROM veiculos v JOIN motoristas m ON m.id = v.motorista_id
        ORDER BY v.placa
    """, con)
    linhas = []
    for _, row in cadastro.iterrows():
        doc = latest.get(int(row.veiculo_id))
        coords = doc["location"]["coordinates"] if doc else [None, None]
        linhas.append({
            "Motorista": row.motorista,
            "Placa": row.placa,
            "Status": row.status_motorista,
            "Temperatura (°C)": doc.get("temperatura") if doc else None,
            "Velocidade (km/h)": doc.get("velocidade") if doc else None,
            "Longitude": coords[0],
            "Latitude": coords[1],
            "Atualizado em (UTC)": doc.get("timestamp") if doc else None,
        })
    return pd.DataFrame(linhas)


def carregar_historico(collection) -> pd.DataFrame:
    docs = list(collection.find({}, {"_id": 0, "veiculo_id": 1, "temperatura": 1, "timestamp": 1}))
    if not docs:
        return pd.DataFrame(columns=["Veículo", "Temperatura (°C)", "Horário"])
    dados = pd.DataFrame(docs)
    dados["Horário"] = pd.to_datetime(dados["timestamp"], utc=True)
    dados["Veículo"] = dados["veiculo_id"].map({101: "ABC-1A23", 102: "XYZ-9876", 103: "KGB-4567"}).fillna(dados["veiculo_id"].astype(str))
    return dados.rename(columns={"temperatura": "Temperatura (°C)"})


def simular(collection, latest: dict[int, dict]) -> None:
    agora = datetime.now(timezone.utc)
    for vehicle_id, doc in latest.items():
        lon, lat = doc["location"]["coordinates"]
        collection.insert_one({
            "veiculo_id": vehicle_id,
            "location": {"type": "Point", "coordinates": [lon + random.uniform(-0.004, 0.004), lat + random.uniform(-0.004, 0.004)]},
            "temperatura": round(float(doc["temperatura"]) + random.uniform(-0.8, 0.8), 1),
            "velocidade": max(0, min(120, int(doc["velocidade"]) + random.randint(-8, 8))),
            "timestamp": agora,
        })


def mapa_frota(lat: float, lon: float, raio_km: float, registros: list[dict]):
    mapa = folium.Map(location=[lat, lon], zoom_start=12, control_scale=True)
    folium.Circle(location=[lat, lon], radius=raio_km * 1000, color="#277da1", fill=True, fill_opacity=0.12, tooltip=f"Raio de busca: {raio_km:g} km").add_to(mapa)
    folium.Marker([lat, lon], tooltip="Ponto de apoio", icon=folium.Icon(color="blue", icon="flag")).add_to(mapa)
    for doc in registros:
        lon_v, lat_v = doc["location"]["coordinates"]
        cor = "red" if float(doc.get("velocidade", 0)) > 80 else "green"
        popup = f"Veículo {doc['veiculo_id']} | {doc.get('velocidade', 0)} km/h | {doc.get('temperatura', 'N/D')} °C"
        folium.Marker([lat_v, lon_v], tooltip=popup, popup=popup, icon=folium.Icon(color=cor, icon="truck", prefix="fa")).add_to(mapa)
    return mapa


def main():
    st.set_page_config(page_title="GeoLog | Monitoramento de Frota", page_icon="🚚", layout="wide")
    st.title("GeoLog - Monitoramento Logístico")
    st.caption("Telemetria geoespacial no MongoDB + cadastro relacional no SQLite")
    con = abrir_sqlite()
    client = None
    try:
        client, collection = conectar_mongodb()
    except (PyMongoError, OSError) as exc:
        st.error("Não foi possível conectar ao MongoDB. Inicie o serviço local ou configure MONGODB_URI e atualize a página.")
        st.code(f"MONGODB_URI atual: {MONGODB_URI}\nDetalhe: {exc}")
        con.close()
        return

    latest = ultimas_leituras(collection)
    dados = dataframe_unificado(con, latest)
    col1, col2, col3 = st.columns(3)
    ativos = int((dados["Status"] == "Ativo").sum())
    medias = dados["Temperatura (°C)"].dropna()
    media_temp = float(medias.mean()) if not medias.empty else 0.0
    alertas = int((dados["Velocidade (km/h)"].fillna(0) > 80).sum())
    col1.metric("Veículos com motorista ativo", ativos)
    col2.metric("Temperatura média", f"{media_temp:.1f} °C")
    col3.metric("Alertas de velocidade (> 80 km/h)", alertas)

    st.subheader("Busca geoespacial por raio")
    left, right = st.columns([1, 2])
    with left:
        ponto_nome = st.selectbox("Ponto de apoio", list(PONTOS_APOIO))
        raio = st.slider("Raio de busca (km)", min_value=1, max_value=100, value=15)
        lat_ref, lon_ref = PONTOS_APOIO[ponto_nome]
    # $near filtra as observacoes na esfera e usa o indice 2dsphere.
    near_cursor = collection.find({"location": {"$near": {"$geometry": {"type": "Point", "coordinates": [lon_ref, lat_ref]}, "$maxDistance": raio * 1000}}})
    candidatos = {doc["veiculo_id"] for doc in near_cursor}
    # Garante que o mapa mostre a ultima posicao conhecida, nao uma leitura historica antiga.
    na_area = []
    for vehicle_id in candidatos:
        doc = latest.get(vehicle_id)
        if not doc:
            continue
        lon, lat = doc["location"]["coordinates"]
        if distancia_km(lat_ref, lon_ref, lat, lon) <= raio:
            na_area.append(doc)
    with left:
        st.write(f"**{len(na_area)}** veículo(s) com última posição dentro do raio.")
    with right:
        st_folium(mapa_frota(lat_ref, lon_ref, raio, na_area), use_container_width=True, height=430, returned_objects=[])

    st.subheader("Visão unificada da frota")
    st.dataframe(dados, use_container_width=True, hide_index=True)

    st.subheader("Análise operacional")
    graph1, graph2 = st.columns(2)
    historico = carregar_historico(collection)
    with graph1:
        if historico.empty:
            st.info("Ainda não há histórico de temperatura.")
        else:
            fig = px.line(historico, x="Horário", y="Temperatura (°C)", color="Veículo", markers=True, title="Temperatura ao longo do tempo")
            st.plotly_chart(fig, use_container_width=True)
    with graph2:
        status = dados["Status"].value_counts().rename_axis("Status do motorista").reset_index(name="Quantidade")
        fig = px.pie(status, names="Status do motorista", values="Quantidade", hole=0.35, title="Status dos motoristas")
        st.plotly_chart(fig, use_container_width=True)

    if st.button("Simular Movimentação", type="primary"):
        simular(collection, latest)
        st.success("Novas leituras gravadas. Atualizando o painel...")
        st.rerun()

    st.caption(f"SQLite: {SQLITE_PATH.name} | MongoDB: {MONGO_DB}.{COLLECTION} | Índice: location_2dsphere")
    con.close()
    client.close()


if __name__ == "__main__":
    main()
