import asyncio
import logging
import os
from contextlib import asynccontextmanager

import certifi
import numpy as np
import pytds
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

from collections import deque
from dataclasses import dataclass, field
from time import monotonic

from uuid import uuid4


load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ----------------------------
# Configurações
# ----------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("Defina GEMINI_API_KEY no arquivo .env")

DB_CONFIG = {
    "server": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "1433")),
    "user": os.getenv("DB_USER", "guiacidadao"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "db-guiacidadao"),
    "timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "30")),
    "login_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "30")),
    "as_dict": True,
}

ssl_disabled = os.getenv("DB_SSL_DISABLED", "false").lower() in ("true", "1", "yes")
if not ssl_disabled:
    DB_CONFIG["cafile"] = os.getenv("DB_SSL_CA") or certifi.where()
    DB_CONFIG["validate_host"] = False


GENERATION_MODEL = "gemini-3.5-flash-lite"
EMBEDDING_MODEL = "gemini-embedding-001"

gemini = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(
        timeout=90000
    )
)
indice = []


# ----------------------------
# Histórico temporário das conversas
# ----------------------------

@dataclass
class SessaoConversa:
    historico: deque = field(
        default_factory=lambda: deque(maxlen=10)
    )
    ultima_atividade: float = field(default_factory=monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# Armazenamento em memória RAM
conversas: dict[str, SessaoConversa] = {}

# Apaga conversas após 30 minutos de inatividade
TEMPO_EXPIRACAO = 30 * 60


def limpar_conversas_expiradas():
    agora = monotonic()

    sessoes_expiradas = [
        session_id
        for session_id, sessao in conversas.items()
        if (
            agora - sessao.ultima_atividade > TEMPO_EXPIRACAO
            and not sessao.lock.locked()
        )
    ]

    for session_id in sessoes_expiradas:
        conversas.pop(session_id, None)


# ----------------------------
# Leitura dos dados no Azure SQL (SQL Server)
# ----------------------------
def buscar_servicos_no_sql():
    with pytds.connect(**DB_CONFIG) as conexao:
        with conexao.cursor() as cursor:
            cursor.execute("""
                SELECT
                    s.id AS servico_id,
                    s.nome,
                    s.descricao,
                    sd.ordem,
                    sd.documento
                FROM servicos s
                LEFT JOIN servicos_documentos sd
                    ON sd.servico_id = s.id
                ORDER BY s.id, sd.ordem
            """)
            linhas = cursor.fetchall()


    servicos = {}

    for linha in linhas:
        servico_id = linha["servico_id"]

        if servico_id not in servicos:
            servicos[servico_id] = {
                "id": servico_id,
                "nome": linha["nome"],
                "descricao": linha["descricao"],
                "documentos": [],
            }

        if linha["documento"]:
            servicos[servico_id]["documentos"].append(linha["documento"])

    return list(servicos.values())


# ----------------------------
# Criação do índice de embeddings
# ----------------------------
def recarregar_indice():
    global indice

    try:
        servicos = buscar_servicos_no_sql()
    except Exception as e:
        logger.warning(
            "Não foi possível carregar serviços do Azure SQL no momento (banco/tabelas podem ainda não estar prontos): %s",
            e,
        )
        return

    if not servicos:
        indice = []
        logger.warning("Nenhum serviço encontrado no Azure SQL.")
        return

    textos = []
    for servico in servicos:
        documentos = ", ".join(servico["documentos"])
        if not documentos:
            documentos = "Não há documentos cadastrados para este serviço."

        texto = (
            f"Serviço: {servico['nome']}\n"
            f"Descrição: {servico['descricao']}\n"
            f"Documentos: {documentos}"
        )
        textos.append(texto)
        servico["texto"] = texto

    resultado = gemini.models.embed_content(
        model=EMBEDDING_MODEL,
        contents=textos,
        config=types.EmbedContentConfig(task_type="RETRIEVAL_DOCUMENT"),
    )


    novo_indice = []
    for servico, embedding in zip(servicos, resultado.embeddings):
        servico["embedding"] = np.array(embedding.values, dtype=np.float32)
        novo_indice.append(servico)

    indice = novo_indice
    logger.info("Índice RAG carregado com %s serviços.", len(indice))


# ----------------------------
# Busca semântica
# ----------------------------
def similaridade_cosseno(vetor_a, vetor_b):
    denominador = np.linalg.norm(vetor_a) * np.linalg.norm(vetor_b)
    if denominador == 0:
        return 0.0
    return float(np.dot(vetor_a, vetor_b) / denominador)


def recuperar_servicos(pergunta, quantidade=3):
    if not indice:
        return []

    resposta_embedding = gemini.models.embed_content(
        model=EMBEDDING_MODEL,
        contents=pergunta,
        config=types.EmbedContentConfig(task_type="RETRIEVAL_QUERY"),
    )
    vetor_pergunta = np.array(
        resposta_embedding.embeddings[0].values,
        dtype=np.float32,
    )

    resultados = []
    for servico in indice:
        pontuacao = similaridade_cosseno(
            vetor_pergunta,
            servico["embedding"],
        )
        resultados.append((pontuacao, servico))

    resultados.sort(key=lambda item: item[0], reverse=True)
    return [servico for _, servico in resultados[:quantidade]]

def montar_consulta_contextual(historico, pergunta):
    partes = []

    for mensagem in historico:
        nome = (
            "Pessoa"
            if mensagem["role"] == "user"
            else "Assistente"
        )

        partes.append(
            f"{nome}: {mensagem['content']}"
        )

    partes.append(f"Pessoa: {pergunta}")

    return "\n".join(partes)


# ----------------------------
# Inicialização da API
# ----------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    
    await asyncio.to_thread(recarregar_indice)
    yield


app = FastAPI(
    title="Guia Cidadão - API de IA",
    description="Assistente de IA de serviços públicos do Guia Cidadão",
    lifespan=lifespan,
)


# ----------------------------
# Endpoint de pergunta
# ----------------------------
class PerguntaRequest(BaseModel):
    pergunta: str = Field(min_length=3, max_length=1000)


@app.post("/perguntar")
async def perguntar(
    dados: PerguntaRequest,
    request: Request,
    response: Response
):
   
    session_id = (
        request.headers.get("X-Session-ID")
        or request.cookies.get("session_id")
    )

    if not session_id:
        session_id = str(uuid4())

    
    response.set_cookie(
        key="session_id",
        value=session_id,
        httponly=True,
        secure=False,
        samesite="lax",
        max_age=TEMPO_EXPIRACAO
    )

    if not indice:
        await asyncio.to_thread(recarregar_indice)

    if not indice:
        raise HTTPException(
            status_code=503,
            detail="Ainda não há serviços cadastrados para consulta.",
        )

    
    limpar_conversas_expiradas()

    
    sessao = conversas.setdefault(
        session_id,
        SessaoConversa()
    )

   
    async with sessao.lock:

        try:
            sessao.ultima_atividade = monotonic()

           
            historico = list(sessao.historico)

    
            while historico and historico[0]["role"] != "user":
                historico.pop(0)

            
            pergunta_contextual = montar_consulta_contextual(
                historico,
                dados.pergunta,
            )

            
            servicos_relevantes = await asyncio.to_thread(
                recuperar_servicos,
                pergunta_contextual,
            )

            contexto = "\n\n".join(
                servico["texto"]
                for servico in servicos_relevantes
            )

     
            instrucoes ="""
                # IDENTIDADE E PAPEL

                Você é o assistente virtual do **Guia Cidadão**, um aplicativo que ajuda os cidadãos a compreenderem e acessarem serviços públicos do Governo do Brasil.

                Sua função é explicar serviços, benefícios, programas, direitos, documentos, requisitos e procedimentos relacionados à administração pública brasileira.

                Seu objetivo é facilitar o acesso à informação pública, utilizando linguagem acessível e orientações claras.

                # 1. IDIOMA E COMUNICAÇÃO

                - Responda sempre em português brasileiro.
                - Utilize linguagem simples, clara, objetiva, paciente e educada.
                - Evite termos técnicos, jurídicos ou burocráticos. Quando forem necessários, explique seus significados.
                - Seja direto e evite explicações desnecessariamente longas.
                - Organize informações em tópicos quando isso facilitar a compreensão.
                - Utilize listas numeradas para apresentar etapas de procedimentos.

                # 2. ESCOPO DE ATUAÇÃO

                Responda somente a perguntas relacionadas a serviços públicos e assuntos de interesse do cidadão no âmbito do Governo brasileiro.

                São assuntos relevantes, entre outros:

                - Benefícios sociais e assistenciais.
                - Previdência e aposentadoria.
                - Saúde pública.
                - Educação.
                - Documentos e registros oficiais.
                - Direitos e deveres dos cidadãos.
                - Programas governamentais.
                - Impostos e obrigações públicas.
                - Trabalho e emprego.
                - Serviços de órgãos públicos.
                - Processos administrativos e atendimento governamental.

                Se o usuário apresentar uma pergunta sem relação com esse escopo, informe educadamente que o Guia Cidadão é especializado em serviços públicos e solicite que reformule a pergunta dentro desse contexto.

                # 3. UTILIZAÇÃO DO CONTEXTO RECUPERADO

                O CONTEXTO RECUPERADO é sua principal fonte de informações factuais.

                Siga estas regras:

                1. Utilize prioritariamente as informações presentes no CONTEXTO RECUPERADO.
                2. Não invente informações para preencher lacunas do contexto.
                3. Não apresente como fato informações que não estejam fundamentadas no contexto ou em conhecimento geral confiável.
                4. Diferencie informações gerais de informações específicas sobre serviços governamentais.
                5. Não invente documentos exigidos, critérios de elegibilidade, valores, prazos, endereços, links, canais de atendimento ou etapas de procedimentos.

                Quando o contexto apresentar informações suficientes, responda normalmente.

                Quando apresentar informações parciais:
                - Responda somente às partes que puder fundamentar.
                - Informe quais informações não estão disponíveis.
                - Não deduza requisitos ou procedimentos que não estejam documentados.

                Quando não apresentar a informação solicitada, informe claramente:

                "Essa informação não consta na base de dados do Guia Cidadão no momento."

                Se for possível, explique o que está disponível no contexto e indique que o usuário consulte o órgão público responsável para confirmar os detalhes.

                # 4. CONHECIMENTO GERAL E LIMITAÇÕES

                Você pode utilizar conhecimento geral para explicar conceitos relacionados a serviços públicos, desde que não apresente informações incertas como procedimentos oficiais.

                Exemplos:
                - Explicar o que é um benefício social.
                - Esclarecer o significado de um termo administrativo.
                - Explicar conceitos gerais sobre direitos dos cidadãos.

                Entretanto, informações específicas e potencialmente variáveis, como valores de benefícios, prazos, requisitos, regras de elegibilidade e procedimentos administrativos, devem estar fundamentadas no CONTEXTO RECUPERADO.

                Se essas informações não estiverem disponíveis, deixe a limitação explícita.

                # 5. UTILIZAÇÃO DO HISTÓRICO

                O HISTÓRICO DE CONVERSA deve ser utilizado exclusivamente para:

                - Compreender perguntas complementares.
                - Identificar referências a assuntos mencionados anteriormente.
                - Recuperar informações necessárias para manter a continuidade da conversa.
                - Evitar solicitar novamente informações que o usuário já forneceu e que não sejam sensíveis.

                Não considere automaticamente que informações antigas continuam válidas.

                Não utilize o histórico como substituto do CONTEXTO RECUPERADO para confirmar regras, valores, prazos ou procedimentos governamentais.

                # 6. SEGURANÇA E PRIVACIDADE

                - Nunca solicite CPF, senhas, números de cartões, dados bancários ou outros dados pessoais sensíveis.
                - Não solicite informações pessoais que não sejam necessárias para explicar um serviço público.
                - Se o usuário fornecer espontaneamente dados sensíveis, não os repita nem os utilize desnecessariamente.
                - Oriente o usuário a não compartilhar informações pessoais ou confidenciais na conversa.
                - Não solicite códigos de autenticação, tokens ou credenciais de acesso.
                - Não tente realizar operações em nome do usuário que exijam autenticação pessoal.

                # 7. CONFIABILIDADE E RESPONSABILIDADE

                - Nunca invente informações, fontes, documentos, requisitos ou procedimentos.
                - Não apresente suposições como fatos.
                - Não garanta que o usuário tenha direito a determinado benefício ou serviço sem informações suficientes.
                - Não afirme que uma solicitação será aprovada ou que determinado procedimento terá resultado garantido.
                - Não se apresente como representante oficial de qualquer órgão governamental.
                - Não trate suas respostas como decisões, pareceres ou determinações oficiais do Governo.
                - Quando houver necessidade de confirmação, recomende consultar o órgão público responsável.

                # 8. PADRÃO DE RESPOSTA

                Sempre que possível, organize as respostas da seguinte maneira:

                1. **Resposta direta:** explique objetivamente o assunto.
                2. **Informações importantes:** apresente requisitos, documentos ou condições disponíveis no contexto.
                3. **Como proceder:** apresente as etapas conhecidas, quando aplicável.
                4. **Observação:** informe eventuais limitações ou informações ausentes na base de dados.

                Não utilize essa estrutura obrigatoriamente quando uma resposta curta for suficiente.

                # 9. REGRA FINAL DE PRIORIDADE

                Em caso de conflito entre instruções, siga esta ordem de prioridade:

                1. Segurança e privacidade do cidadão.
                2. Confiabilidade e veracidade das informações.
                3. Respeito ao escopo de atuação do Guia Cidadão.
                4. Utilização do CONTEXTO RECUPERADO como fonte factual prioritária.
                5. Continuidade da conversa por meio do HISTÓRICO.
                6. Clareza, simplicidade e objetividade da resposta.

                **REGRA FUNDAMENTAL:** É preferível informar que uma informação não está disponível a fornecer uma orientação incorreta ao cidadão.
                """

            
            conteudos = []

            for mensagem in historico:
                conteudos.append(
                    types.Content(
                        role=mensagem["role"],
                        parts=[
                            types.Part.from_text(
                                text=mensagem["content"]
                            )
                        ],
                    )
                )

           
            mensagem_atual = f"""
            CONTEXTO RECUPERADO DO BANCO DE DADOS:

            {contexto}

            PERGUNTA ATUAL DA PESSOA:

            {dados.pergunta}
            """

            conteudos.append(
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_text(
                            text=mensagem_atual
                        )
                    ],
                )
            )

            logger.info(
                "Enviando pergunta para o Gemini. Sessão: %s",
                session_id
            )

           
            resposta_gemini = await asyncio.to_thread(
                gemini.models.generate_content,
                model=GENERATION_MODEL,
                contents=conteudos,
                config=types.GenerateContentConfig(
                    max_output_tokens=512,
                    system_instruction=instrucoes,
                ),
            )

            resposta = (
                resposta_gemini.text
                or "Não foi possível gerar uma resposta."
            )

            logger.info("Gemini finalizou a resposta.")

           
            sessao.historico.append({
                "role": "user",
                "content": dados.pergunta,
            })

            sessao.historico.append({
                "role": "model",
                "content": resposta,
            })

            sessao.ultima_atividade = monotonic()

            
            fontes = [
                {
                    "servico_id": servico["id"],
                    "servico": servico["nome"],
                }
                for servico in servicos_relevantes
            ]

            return {
                "session_id": session_id,
                "resposta": resposta,
                "fontes": fontes
            }

        except Exception as e:
            logger.exception("Erro ao processar pergunta")

            codigo = getattr(e, "code", None)

            if codigo in (429, 503, 504):
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "A IA está temporariamente indisponível. "
                        "Tente novamente."
                    ),
                ) from e

            raise HTTPException(
                status_code=502,
                detail="Não foi possível consultar a IA neste momento.",
            ) from e


@app.post("/recarregar")
async def recarregar():
    await asyncio.to_thread(recarregar_indice)
    return {
        "status": "ok",
        "servicos_indexados": len(indice),
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "servicos_indexados": len(indice),
    }
