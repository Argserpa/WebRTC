#!/usr/bin/env python3
"""
Generador de carga para el Proyecto 2 (WebRTC).

Abre N receptores WebRTC reales (aiortc, recvonly) contra el endpoint /offer del
video-streamer y los mantiene conectados hasta Ctrl-C. Sirve para las pruebas de
escalado E2: medir CPU / memoria / coste del servidor frente a N peers, con
conexiones equivalentes a las de un navegador (mismo trabajo de SRTP/DTLS y misma
suscripción al MediaRelay en el servidor).

Uso A — dentro de un pod del cluster (localhost:8081 = streamer):
    kubectl cp monitoring/loadgen-webrtc.py streaming/<pod>:/tmp/loadgen.py
    kubectl exec -it -n streaming <pod> -- python /tmp/loadgen.py 10

    OJO: si el pod es el PROPIO video-streamer, los N receptores viven en el mismo
    cgroup que FFmpeg y pueden provocar un OOM del pod y contaminar la medida de
    CPU/memoria. Para N alto lánzalo desde OTRO pod o desde la LAN (Uso B).

Uso B — desde otra máquina de la LAN que alcance el signaling:
    python loadgen-webrtc.py 10 http://<host>:8081

Argumentos:
    N       número de receptores (por defecto 5)
    BASE    URL base del signaling (por defecto http://localhost:8081)
    RAMP    segundos entre el alta de cada peer (por defecto 0.2). Escalonar evita
            reventar el gathering ICE del servidor con una ráfaga simultánea.

NOTA de implementación: la señalización va por aiohttp (asíncrono). La versión
anterior usaba urllib.urlopen (BLOQUEANTE), que serializaba las offers y congelaba
el event loop, impidiendo que las conexiones ICE de los peers ya creados avanzaran;
por eso la carga se quedaba clavada en ~13 aunque el servidor tuviese margen.

CAVEAT de red (importante para la métrica "egress vs N"):
    Si los receptores corren en la MISMA máquina que el streamer (loopback), sí
    ejercitan CPU/memoria/SRTP del servidor, pero el tráfico va por 'lo' y NO
    aparece en node_network_*{device!="lo"}. Para medir egress real en la NIC hay
    que lanzar los receptores desde OTRA(S) máquina(s) de la LAN (Uso B).
"""
import asyncio
import sys
import aiohttp
from aiortc import RTCPeerConnection, RTCSessionDescription

N = int(sys.argv[1]) if len(sys.argv) > 1 else 5
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8081"
RAMP = float(sys.argv[3]) if len(sys.argv) > 3 else 0.2


async def _drain(track):
    """Consume y descarta frames como haría un navegador (que los pinta).

    IMPRESCINDIBLE: si no se drena el track, los frames entrantes se acumulan sin
    límite en el receptor Y obligan al MediaRelay del servidor a bufferizar la
    salida → la memoria de AMBOS lados crece hasta OOM. Drenar mantiene el buffer
    acotado y hace que la carga sea equivalente a la de un cliente real.
    """
    try:
        while True:
            await track.recv()
    except Exception:
        pass


async def receiver(session, pcs, idx):
    """Abre un receptor recvonly y negocia contra /offer (todo asíncrono)."""
    pc = RTCPeerConnection()
    pc.addTransceiver("video", direction="recvonly")
    pc.addTransceiver("audio", direction="recvonly")

    @pc.on("track")
    def on_track(track):
        asyncio.ensure_future(_drain(track))   # drenar en background cada track recibido

    pcs.append(pc)
    await pc.setLocalDescription(await pc.createOffer())
    body = {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
    try:
        async with session.post(BASE + "/offer", json=body,
                                timeout=aiohttp.ClientTimeout(total=30)) as resp:
            ans = await resp.json()
        await pc.setRemoteDescription(RTCSessionDescription(sdp=ans["sdp"], type=ans["type"]))
    except Exception as e:
        print(f"  peer {idx} error: {e}")


async def main():
    pcs = []
    print(f"Abriendo {N} receptores WebRTC contra {BASE} (ramp {RAMP}s/peer) ...")
    async with aiohttp.ClientSession() as session:
        # Lanzar cada receptor como tarea y escalonar el alta con RAMP: así el
        # event loop procesa el ICE de todos en paralelo mientras se dan de alta.
        tasks = []
        for i in range(N):
            tasks.append(asyncio.create_task(receiver(session, pcs, i)))
            await asyncio.sleep(RAMP)
        await asyncio.gather(*tasks, return_exceptions=True)

        # Ventana de estabilización: los peers pasan a 'connected' de forma escalonada.
        for _ in range(6):
            await asyncio.sleep(5)
            up = sum(1 for p in pcs if p.connectionState == "connected")
            print(f"  conectados: {up}/{N}")

        print(f"{sum(1 for p in pcs if p.connectionState == 'connected')}/{N} conectados. "
              f"Comprueba TFG_webrtc_peers en Grafana. Ctrl-C para cerrar.")
        try:
            while True:
                await asyncio.sleep(5)
                print("  conectados:", sum(1 for p in pcs if p.connectionState == "connected"))
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await asyncio.gather(*[p.close() for p in pcs], return_exceptions=True)
            print("cerrados.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
