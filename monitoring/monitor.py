import asyncio
import aiohttp
from collections import defaultdict, deque
from datetime import datetime
from bot.utils import escape_markdown_v2, send_telegram_message
from config import (
    API_ROULETTE_PASSWORD,
    API_ROULETTE_RESULTS_URL,
    API_ROULETTE_USER,
    HISTORICO_MAX,
    ROULETTES,
)

BLACK_SNAKE = [2, 6, 10, 13, 17, 24, 26, 28, 31, 35]
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
    filtrado = {k: v for k, v in tendencias.items() if v["total"] >= MINIMO_OCORRENCIAS}
    return sorted(
        filtrado.items(), key=lambda x: (-x[1]["porcentagem"], -x[1]["total"])
    )[:n]


def formatar_tendencias_console(
    roulette_id, top_tendencias, tendencias, historico_size
):
    header = (
        f"\n=== TENDÊNCIAS {roulette_id} === (Últimas {historico_size} rodadas) ==="
    )
    print(header)
    if not top_tendencias:
        print("Aguardando dados suficientes para análise...")
        return
    for i, (num, stats) in enumerate(top_tendencias, 1):
        print(
            f"{i}º - Número {num}: {stats['porcentagem']}% (BLACK_SNAKE em{stats['chamou_black_snake']}/{stats['total']})"
        )
    print("=" * len(header.split("\n")[0]))


async def notificar_entrada(roulette_id, numero, tendencias):
    stats = tendencias[numero]
    message = (
        f"🔥 ENTRADA CONFIRMADA! 🔥\n\n"
        f"🎰 Mesa: {escape_markdown_v2(roulette_id)} - Playtech\n"
        f"🎯 Número: {numero}\n"
        f"📈 Tendência: {stats['porcentagem']}% (BLACK_SNAKE em{stats['chamou_black_snake']}/{stats['total']})\n"
        f"🔍 BLACK_SNAKE esperado após este número"
    )
    await send_telegram_message(message, LINK_MESA_BASE)


async def enviar_tendencias_telegram(
    roulette_id, top_tendencias, tendencias, historico_size
):
    message = "📊 *TENDÊNCIAS ATUALIZADAS* 📊\n\n"
    message += "⚠️ *BLACK SNAKE* ⚠️\n\n"
    message += f"🎰 Mesa: {escape_markdown_v2(roulette_id)} - Playtech\n\n"
    for i, (num, stats) in enumerate(top_tendencias, 1):
        message += f"{i}º - Número *{num}*: _{stats['porcentagem']}%_ ({stats['chamou_black_snake']}/{stats['total']})\n\n"
    message += "\n🔔 Entradas confirmadas quando estes números aparecerem!"
    await send_telegram_message(message)


async def fetch_results_http(session):
    auth = aiohttp.BasicAuth(API_ROULETTE_USER, API_ROULETTE_PASSWORD)
    async with session.get(API_ROULETTE_RESULTS_URL, auth=auth) as resp:
        resp.raise_for_status()
        data = await resp.json()
        resultados = data.get("results") or []
        out = []
        for r in resultados:
            num = r.get("number")
            if num is None:
                continue
            s = str(num).strip()
            if s.isdigit():
                out.append(int(s))
        return out


async def monitor_roulette(roulette_id):
    print(f"[INICIANDO] Monitorando mesa: {roulette_id}")
    mesa = estado_mesas[roulette_id]
    mesa["notificacao_inicial_enviada"] = False
    mesa["ultima_porcentagem_top"] = {}

    async with aiohttp.ClientSession() as session:
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
                        }
                    )

                resultados = await fetch_results_http(session)
                if not resultados:
                    await asyncio.sleep(2)
                    continue

                mesa["historico"] = deque(
                    resultados[:HISTORICO_COMPLETO_SIZE], maxlen=HISTORICO_COMPLETO_SIZE
                )
                historico_size = len(mesa["historico"])
                mesa["contador_rodadas"] += 1

                if historico_size >= MINIMO_RODADAS_ANALISE:
                    nova_tendencia = analisar_tendencias(mesa["historico"])
                    novo_top = get_top_tendencias(nova_tendencia)
                    novo_top_numeros = [num for num, _ in novo_top]

                    mudou_porcentagem = any(
                        mesa["ultima_porcentagem_top"].get(num)
                        != nova_tendencia[num]["porcentagem"]
                        for num in novo_top_numeros
                    )

                    if not mesa["notificacao_inicial_enviada"]:
                        mesa["top_tendencias"] = novo_top_numeros
                        mesa["tendencias"] = nova_tendencia
                        mesa["ultima_porcentagem_top"] = {
                            num: nova_tendencia[num]["porcentagem"]
                            for num in novo_top_numeros
                        }
                        formatar_tendencias_console(
                            roulette_id, novo_top, nova_tendencia, historico_size
                        )
                        await enviar_tendencias_telegram(
                            roulette_id, novo_top, nova_tendencia, historico_size
                        )
                        mesa["notificacao_inicial_enviada"] = True

                    elif mesa["contador_rodadas"] % TENDENCIA_UPDATE_INTERVAL == 0 and (
                        novo_top_numeros != mesa["top_tendencias"] or mudou_porcentagem
                    ):
                        mesa["top_tendencias"] = novo_top_numeros
                        mesa["tendencias"] = nova_tendencia
                        mesa["ultima_porcentagem_top"] = {
                            num: nova_tendencia[num]["porcentagem"]
                            for num in novo_top_numeros
                        }
                        formatar_tendencias_console(
                            roulette_id, novo_top, nova_tendencia, historico_size
                        )
                        await enviar_tendencias_telegram(
                            roulette_id, novo_top, nova_tendencia, historico_size
                        )

                await asyncio.sleep(10)

            except Exception as e:
                print(f"[ERRO] {roulette_id}: {str(e)}")
                await asyncio.sleep(5)


async def start_all():
    tasks = [asyncio.create_task(monitor_roulette(mesa)) for mesa in ROULETTES]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(start_all())
