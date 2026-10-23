import random
from pathlib import Path

from agents.config import AgentKind

CUSTOMER_QUESTIONS = (
    "No consigo entrar en la app, que pasos generales puedo seguir?",
    "Como puedo bloquear una tarjeta que he perdido?",
    "Donde puedo consultar un justificante de transferencia?",
    "He recibido un SMS sospechoso sobre mi cuenta, que hago?",
    "Como cambio las notificaciones de la app?",
    "Que debo revisar antes de confirmar una transferencia?",
    "No reconozco un cargo en mi tarjeta, como debo actuar?",
    "Como puedo recuperar el acceso sin compartir mis claves?",
)


def random_prompt(kind: AgentKind, rng: random.Random, sections: int = 60) -> str:
    if kind == "customer":
        return rng.choice(CUSTOMER_QUESTIONS)
    if sections < 1:
        raise ValueError("Document sections must be positive")
    company = f"Empresa Sintetica {rng.randrange(1000, 10000)}"
    documents = [
        f"DOCUMENTO SINTETICO - {company}. Todas las cifras y entidades son ficticias.",
        "Cifras en millones de EUR. No contiene datos de clientes reales.",
    ]
    for section in range(1, sections + 1):
        revenue = rng.randint(80, 900)
        debt = rng.randint(30, 600)
        cash = rng.randint(5, 150)
        ebitda = round(revenue * rng.uniform(0.05, 0.28), 2)
        documents.append(
            f"Seccion {section}: unidad {section} de {company}, "
            f"sector {rng.choice(('energia', 'industria', 'transporte', 'tecnologia'))}. "
            f"Ventas anuales: {revenue}; EBITDA: {ebitda}; deuda bruta: {debt}; "
            f"caja disponible: {cash}; vencimientos a 12 meses: {rng.randint(10, 100)}. "
            f"El principal cliente concentra el {rng.randint(10, 65)}% de los ingresos. "
            f"El {rng.randint(15, 85)}% de la deuda tiene tipo variable y "
            f"el {rng.randint(5, 70)}% de las ventas esta expuesto a divisas. "
            f"El escenario adverso asume una caida de ventas del {rng.randint(5, 30)}%, "
            f"un aumento de tipos de {rng.choice((100, 150, 200, 250))} puntos basicos "
            "y una demora de cobros de 30 dias. La direccion propone reducir inversiones "
            "no esenciales y renegociar vencimientos. No se aporta valoracion independiente "
            "de garantias ni evidencia de lineas de liquidez comprometidas. "
            "La informacion es una instantanea separada por unidad; no sumes porcentajes "
            "y explica las limitaciones de consolidacion."
        )
    return (
        "Evalua el riesgo de esta operacion ficticia. Presenta escenarios base y adverso, "
        "cita secciones concretas, indica datos ausentes y recomienda mitigantes.\n\n"
        + "\n\n".join(documents)
    )


def with_documents(prompt: str, paths: list[Path]) -> str:
    documents = []
    for path in paths:
        if path.suffix.lower() not in {".txt", ".md"}:
            raise ValueError(f"Only UTF-8 .txt and .md documents are supported: {path}")
        documents.append(
            f"\n\n<document name={path.name!r}>\n{path.read_text(encoding='utf-8')}\n</document>"
        )
    return prompt + "".join(documents)
