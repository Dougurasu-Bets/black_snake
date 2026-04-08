import asyncio
import aiohttp
from collections import defaultdict, deque
from datetime import datetime
from bot.utils import escape_markdown_v2, send_telegram_message
from config import (
    API_ROULETTE_PASSWORD,
    API_ROULETTE_RESULTS_URL,
    API_ROULETTE_USER,
    ROULETTES,
)

BLACK_SNAKE = [2, 6, 8, 10, 13, 17, 20, 24, 26, 28, 31, 35]
HISTORICO_COMPLETO_SIZE = 500
TENDENCIA_UPDATE_INTERVAL = 10
MINIMO_OCORRENCIAS = 5
MINIMO_RODADAS_ANALISE = 50

LINK_MESA_BASE = "https://geralbet.bet.br/live-casino/game/3763038"

estado_mesas = defaultdict(
    lambda: {
        "status": "idle",
        "entrada": None,
        "gale": 0,
        "monitorando": False,
        "greens": 0,
        "greens_g1": 0,
        "greens_g2": 0,
        "loss": 0,
        "total": 0,
        "consec_greens": 0,
        "ultimo_resultado_validado": None,
        "data_atual": datetime.now().date(),
        "historico": deque(maxlen=HISTORICO_COMPLETO_SIZE),
        "sinais_enviados": 0,
        "aguardando_confirmacao": False,
        "tendencias": {},
        "top_tendencias": [],
        "contador_rodadas": 0,
        "ultima_atualizacao_tendencias": None,
        "entrada_ativa": False,
        "numero_entrada": None,
        "gale": 0,
    }
)


def pertence_ao_padrao(numero):
    return numero in BLACK_SNAKE


def analisar_tendencias(historico):
    historico = list(historico)
    tendencias = {n: {"chamou_black_snake": 0, "total": 0} for n in range(37)}

    for idx in range(3, len(historico)):
        numero_atual = historico[idx]
        anteriores = historico[idx - 3 : idx][::-1]

        for anterior in anteriores:
            if pertence_ao_padrao(anterior):
                tendencias[numero_atual]["chamou_black_snake"] += 1
                break

        tendencias[numero_atual]["total"] += 1

    for numero in tendencias:
        total = tendencias[numero]["total"]
        chamou_black_snake = tendencias[numero]["chamou_black_snake"]
        porcentagem = round((chamou_black_snake / total * 100), 2) if total > 0 else 0
        tendencias[numero]["porcentagem"] = porcentagem

    return tendencias


def get_top_tendencias(tendencias, n=10):
    filtrado = {
        k: v
        for k, v in tendencias.items()
        if v["total"] >= MINIMO_OCORRENCIAS and v["porcentagem"] >= 80
    }
    return sorted(filtrado.items(), key=lambda x: -x[1]["porcentagem"])[:n]



async def notificar_entrada(roulette_id, numero, tendencias):
    stats = tendencias[numero]
    message = f"🔥 ENTRADA Padrão BLACK SNAKE - {numero} ({stats['chamou_black_snake']}/{stats['total']})\n"
    await send_telegram_message(message, LINK_MESA_BASE)


async def fetch_results_http(session, mesa_nome):
    """
    Resposta esperada:
    provider, tableSlug, results[{number, resultMultiplier?}], tableName, launchAlias
    """
    async with session.get(API_ROULETTE_RESULTS_URL) as resp:
        resp.raise_for_status()
        data = await resp.json()

    if data.get("tableName") and data["tableName"] != mesa_nome:
        print(
            f"[AVISO] tableName API={data.get('tableName')!r} != config={mesa_nome!r}"
        )

    raw = data.get("results") or []
    numeros = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        n = r.get("number")
        try:
            numero = int(n)
        except (TypeError, ValueError):
            continue
        numeros.append(
            {"number": numero, "multiplier": r.get("resultMultiplier")}
        )
    return numeros


async def monitor_roulette(roulette_id):
    print(f"[INICIANDO] Monitorando mesa: {roulette_id}")
    mesa = estado_mesas[roulette_id]
    mesa["notificacao_inicial_enviada"] = False
    mesa["ultima_porcentagem_top"] = {}
    mesa["entrada_ativa"] = False
    mesa["numero_entrada"] = None
    mesa["gale"] = 0
    mesa["ultimo_numero_processado"] = None

    auth = aiohttp.BasicAuth(API_ROULETTE_USER, API_ROULETTE_PASSWORD)
    async with aiohttp.ClientSession(auth=auth) as session:
        while True:
            try:
                hoje = datetime.now().date()
                if mesa["data_atual"] != hoje:
                    mesa.update(
                        {
                            "greens": 0,
                            "greens_g1": 0,
                            "greens_g2": 0,
                            "loss": 0,
                            "total": 0,
                            "consec_greens": 0,
                            "data_atual": hoje,
                            "sinais_enviados": 0,
                            "notificacao_inicial_enviada": False,
                            "ultima_porcentagem_top": {},
                            "contador_rodadas": 0,
                        }
                    )

                resultados = await fetch_results_http(session, roulette_id)
                if not resultados:
                    await asyncio.sleep(2)
                    continue

                mesa["historico"] = deque(
                    resultados[:HISTORICO_COMPLETO_SIZE], maxlen=HISTORICO_COMPLETO_SIZE
                )
                historico_size = len(mesa["historico"])
                mesa["contador_rodadas"] += 1

                if historico_size >= MINIMO_RODADAS_ANALISE:
                    nova_tendencia = analisar_tendencias(
                        [item["number"] for item in mesa["historico"]]
                    )
                    novo_top = get_top_tendencias(nova_tendencia)
                    novo_top_numeros = [num for num, _ in novo_top]

                    mesa["tendencias"] = nova_tendencia
                    mesa["top_tendencias"] = novo_top_numeros
                    mesa["ultima_porcentagem_top"] = {
                        num: nova_tendencia[num]["porcentagem"]
                        for num in novo_top_numeros
                    }

                    numero_atual = mesa["historico"][0]["number"]
                    multiplicador_atual = mesa["historico"][0].get("multiplier")

                    if numero_atual == mesa["ultimo_numero_processado"]:
                        await asyncio.sleep(2)
                        continue

                    mesa["ultimo_numero_processado"] = numero_atual

                    if not mesa["entrada_ativa"] and numero_atual in novo_top_numeros:
                        mesa["entrada_ativa"] = True
                        mesa["numero_entrada"] = numero_atual
                        mesa["gale"] = 0
                        
                        await notificar_entrada(
                            roulette_id, numero_atual, nova_tendencia
                        )

                    elif mesa["entrada_ativa"]:
                        if pertence_ao_padrao(numero_atual):
                            mesa["greens"] += 1
                            mesa["total"] += 1
                            if mesa["gale"] == 1:
                                mesa["greens_g1"] += 1
                            elif mesa["gale"] == 2:
                                mesa["greens_g2"] += 1

                            mult_msg = (
                                f"😱😱😱 PAGOU {multiplicador_atual}x"
                                if multiplicador_atual
                                else ""
                            )
                            n1 = mesa["historico"][0]["number"]
                            n2 = mesa["historico"][1]["number"]
                            n3 = mesa["historico"][2]["number"]
                            await send_telegram_message(
                                f"✅✅✅ GREEN!!!\n\n{mult_msg}\n({n1}|{n2}|{n3})"
                            )

                            mesa["entrada_ativa"] = False
                            mesa["numero_entrada"] = None
                            mesa["gale"] = 0

                        elif mesa["gale"] == 0:
                            mesa["gale"] = 1
                            await send_telegram_message(
                                f"🔁 Primeiro GALE ({numero_atual})"
                            )
                        elif mesa["gale"] == 1:
                            mesa["gale"] = 2
                            await send_telegram_message(
                                f"🔁 Segundo e último GALE ({numero_atual})"
                            )
                        else:
                            mesa["loss"] += 1
                            mesa["total"] += 1

                            n1 = mesa["historico"][0]["number"]
                            n2 = mesa["historico"][1]["number"]
                            n3 = mesa["historico"][2]["number"]
                            await send_telegram_message(
                                f"❌❌❌ LOSS!!! ❌❌❌ \n\n({n1}|{n2}|{n3})"
                            )

                            mesa["entrada_ativa"] = False
                            mesa["numero_entrada"] = None
                            mesa["gale"] = 0

                await asyncio.sleep(2)

            except Exception as e:
                print(f"[ERRO] {roulette_id}: {str(e)}")
                await asyncio.sleep(5)


async def start_all():
    tasks = [asyncio.create_task(monitor_roulette(mesa)) for mesa in ROULETTES]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(start_all())
