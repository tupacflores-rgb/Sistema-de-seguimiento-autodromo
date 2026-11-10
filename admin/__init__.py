"""Página de administrador de MULTICAM VISION.

Secciones: Cámaras, Video en vivo, Configuración, Pista (con dibujo vinculado), Vehículos,
Tracking (con un "Modo demo" que simula autos y una carrera sin cámaras ni hardware),
ESP32 y semáforo. Además, `/display`: pantalla de solo lectura (posiciones y velocidad)
pensada para abrir en un teléfono o tablet aparte y mostrarla al público.
Dependencias: Python >= 3.10 y Flask. El tracking real además usa OpenCV y NumPy (el modo demo
NO las necesita). El modo "hardware" de la sección ESP32 además usa pyserial (no hace falta en
modo "simulation").
"""

__version__ = "0.9.0"
