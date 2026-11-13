# tests/test_sin_remesas.py — R8A NO ofrece remesas, solo recargas telefónicas.
# Naylan llegó a ofrecer "recargas, remesas y vuelos" como servicios de Cuba.
# Quitar la palabra del prompt no basta: el modelo sabe lo que es una remesa y
# podría ofrecerla igual, así que hace falta una prohibición explícita.

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-key"


def _prompt() -> str:
    from agent.brain import cargar_system_prompt
    return cargar_system_prompt()


def _seccion(titulo: str, fin: str) -> str:
    """Extrae una sección del prompt, entre dos marcadores."""
    prompt = _prompt()
    i = prompt.index(titulo)
    j = prompt.index(fin, i + len(titulo))
    return prompt[i:j]


def test_01_la_lista_de_servicios_no_ofrece_remesas():
    """La enumeración de servicios de R8A no puede incluir remesas."""
    servicios = _seccion("## Servicios de R8A", "## Equipo R8A")
    assert "remesa" not in servicios.lower(), (
        f"la lista de servicios todavía menciona remesas:\n{servicios}"
    )
    # Las recargas telefónicas sí siguen ofreciéndose
    assert "recargas telefónicas" in servicios.lower()


def test_01b_los_datos_a_recopilar_no_incluyen_remesas():
    """Naylan no debe recopilar datos para una remesa."""
    datos = _seccion("**Datos a recopilar por servicio:**", "**Respuesta al completar")
    assert "remesa" not in datos.lower(), f"aún pide datos de remesa:\n{datos}"


def test_02_cuba_solo_menciona_recargas_telefonicas():
    prompt = _prompt().lower()
    # La seccion de datos a recopilar para Cuba no debe pedir datos de remesa
    assert "cuba (recargas/remesas)" not in prompt
    assert "recargas" in prompt, "las recargas telefónicas sí se ofrecen"


def test_03_existe_prohibicion_explicita_de_remesas():
    """Debe haber una instrucción que le prohíba ofrecerlas e indique qué responder."""
    prompt = _prompt()
    assert re.search(r"(NUNCA|NO).{0,120}remesa", prompt, re.IGNORECASE | re.DOTALL), (
        "falta la regla que prohíbe ofrecer remesas"
    )
    assert "no ofrece" in prompt.lower() or "no ofrecemos" in prompt.lower()


def test_04_el_escalamiento_ya_no_menciona_tasa_de_remesa():
    assert "tasa de remesa" not in _prompt().lower()
