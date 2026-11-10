#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auditor_ia.py · Agente de Auditoria de Fretes com IA
Projeto de portfólio: https://github.com/Amaro-bit/auditoria-fretes-ia

O QUE O SCRIPT FAZ, EM 6 PASSOS
  1. Lê três planilhas CSV com Pandas:
       - Fatura da transportadora: os CT-es cobrados (valor, peso taxado, taxas, serviço).
       - Relatório do Power BI: a base interna do SAP (NF, rota, peso real, cubagem,
         valor da mercadoria e frete previsto).
       - Cotações spot registradas no WhatsApp (opcional).
  2. Cruza a fatura com o Power BI pelo número da NF (é um PROCV, ou um LEFT JOIN em SQL).
  3. Recalcula o frete pela regra comercial de cada transportadora (4 modalidades).
  4. Valida: duplicidade, ICMS, faixa de peso, cubagem, ad valorem, GRIS, serviço e cotação.
  5. Envia só as divergências para um LLM (Claude ou Gemini) redigir o parecer e a defesa
     técnica. Os números ficam travados: quem calcula é o código; a IA confere e redige.
  6. Salva a planilha padronizada (.csv), os pareceres (.jsonl) e as defesas (.txt).

PRINCÍPIO DE ARQUITETURA
  Os valores nunca vêm do LLM. O motor de regras é determinístico (mesma entrada, mesma
  saída) e entrega ao LLM os números já calculados. Quando a resposta volta, o script confere
  campo a campo: se a IA alterou algum valor, o valor do motor é mantido e a divergência
  fica registrada no próprio parecer.

COMO USAR
  pip install pandas
  python auditor_ia.py --fatura fatura.csv --power-bi relatorio_bi.csv --cotacoes cotacoes.csv
  python auditor_ia.py ... --provedor claude    (exige a variável de ambiente ANTHROPIC_API_KEY)
  python auditor_ia.py ... --provedor gemini    (exige a variável de ambiente GEMINI_API_KEY)
  Sem --provedor, o modo é "offline": o próprio motor redige o parecer, sem internet e sem custo.

As regras e os números são idênticos aos do index.html (o painel do projeto), então o mesmo
CSV produz o mesmo resultado nas duas pontas. Contratos, notas e transportadoras são fictícios.
"""

# =============================================================================
# 0. BIBLIOTECAS
# =============================================================================
# Só o Pandas precisa ser instalado; todo o resto já vem com o Python.
import argparse                       # lê os argumentos da linha de comando (--fatura, --power-bi...)
import io                             # transforma um texto em "arquivo em memória" para o Pandas ler
import json                           # converte dicionários Python em JSON e JSON em dicionários
import math                           # floor (arredondar para baixo) e isfinite (é um número válido?)
import os                             # lê variáveis de ambiente, onde ficam as chaves de API
import re                             # expressões regulares (buscar padrões em textos)
import sys                            # epsilon do float e saída de erro (stderr)
import time                           # espera entre tentativas quando a API falha (backoff)
import unicodedata                    # separa letras e acentos para podermos remover os acentos
import urllib.error                   # tipos de erro das chamadas HTTP
import urllib.request                 # faz chamadas HTTP sem instalar biblioteca extra
from datetime import datetime, timezone   # data da auditoria e carimbo de horário
from decimal import Decimal, ROUND_HALF_UP  # arredondamento exato de centavos, como no Excel
from pathlib import Path              # caminhos de arquivo que funcionam no Windows, Mac e Linux

import pandas as pd                   # leitura das planilhas, cruzamento (merge) e resumos (groupby)


# =============================================================================
# 1. CONFIGURAÇÃO DE NEGÓCIO (os mesmos valores do index.html)
# =============================================================================
TOLERANCIA_ABS = 1.00                 # R$: diferença de até R$ 1,00 é arredondamento, não erro
TOLERANCIA_PCT = 0.005                # 0,5% do valor de referência (vale o maior dos dois)
TOLERANCIA_COMPONENTE = 0.50          # R$: folga por componente (frete-peso, ad valorem, GRIS)
TOLERANCIA_PESO = 0.05                # 5% de folga sobre o peso taxável antes de acusar erro
ALCADA_REVISAO_HUMANA = 1000.00       # glosa a partir deste valor precisa de um humano aprovando
LIMITE_CHAMADAS_LLM = 30              # teto de chamadas pagas por execução (controle de custo)
VERSAO_SCHEMA = 'parecer_auditoria_v3'  # nome do formato JSON do parecer (igual ao do painel)

# Modelos padrão de cada provedor (dá para trocar com --modelo).
# Claude: lista oficial em https://docs.claude.com/en/docs/about-claude/models
# Gemini: "gemini-flash-latest" é um apelido que aponta para o Flash mais recente.
#         Em produção, prefira fixar uma versão estável (https://ai.google.dev/gemini-api/docs/models).
MODELO_PADRAO = {'claude': 'claude-sonnet-5-5', 'gemini': 'gemini-flash-latest'}

# Endereços das APIs (documentação: https://docs.claude.com/en/api/messages e
# https://ai.google.dev/api/generate-content)
URL_CLAUDE = 'https://api.anthropic.com/v1/messages'
URL_GEMINI = 'https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent'

# Marcadores que delimitam o prompt dentro do system_prompt_agente.md. O texto fora deles é
# documentação para humanos e não é enviado ao modelo.
MARCADOR_INICIO = '<!-- INICIO DO PROMPT -->'
MARCADOR_FIM = '<!-- FIM DO PROMPT -->'


# =============================================================================
# 2. CONTRATOS POR TRANSPORTADORA (fonte 3 do cruzamento; fictícios por sigilo)
# =============================================================================
# O contrato diz QUAL regra de cálculo vale para cada transportadora e com quais parâmetros.
CONTRATOS = {
    # Modalidade 1: tabela combinada por rota e faixa de peso, valores SEM ICMS
    'Transportadora Alfa': {
        'modalidade': 'TABELA_ICMS',
        'modal': 'RODOVIARIO',
        'fator_cubagem': 300,                          # kg por m³ (padrão do rodoviário)
        'faixas_kg': [100, 300, 500, 1000, 2000, 5000],  # limite superior de cada faixa
        'tabela': {                                     # rota → um valor para cada faixa
            'SP-GO': [168.00, 289.00, 418.00, 702.00, 1185.00, 2240.00],
            'GO-SP': [161.00, 277.00, 401.00, 674.00, 1138.00, 2150.00],
            'GO-DF': [92.00, 159.00, 230.00, 386.00, 652.00, 1232.00],
            'MG-BA': [188.00, 324.00, 468.00, 786.00, 1327.00, 2509.00],
            'SP-RJ': [124.00, 214.00, 309.00, 519.00, 877.00, 1658.00],
            'GO-MG': [136.00, 234.00, 339.00, 569.00, 960.00, 1814.00],
        },
    },
    # Modalidade 2: calculadora própria (reproduzida em massa): frete-peso + ad valorem + GRIS
    'Beta Cargas': {
        'modalidade': 'CALCULADORA', 'modal': 'RODOVIARIO', 'fator_cubagem': 300,
        'tarifa_kg': 0.62,              # R$ por kg taxado
        'frete_peso_minimo': 120.00,    # valor mínimo do frete-peso
        'ad_valorem_pct': 0.30,         # seguro: % sobre o valor da mercadoria
        'gris_pct': 0.15,               # gerenciamento de risco: % sobre o valor da mercadoria
        'gris_minimo': 8.50,            # valor mínimo do GRIS
    },
    'Gama Logística': {
        'modalidade': 'CALCULADORA', 'modal': 'RODOVIARIO', 'fator_cubagem': 300,
        'tarifa_kg': 0.55, 'frete_peso_minimo': 140.00,
        'ad_valorem_pct': 0.25, 'gris_pct': 0.20, 'gris_minimo': 10.00,
    },
    # Modalidade 3: seletor de serviço (aéreo), uma tarifa por nível de serviço
    'Delta Express': {
        'modalidade': 'SELETOR_SERVICO', 'modal': 'AEREO',
        'fator_cubagem': 167,           # aéreo: 6.000 cm³ por kg (padrão IATA) ≈ 167 kg/m³
        'servicos': {
            'ECOMMERCE': {'tarifa_kg': 3.10, 'minimo': 45.00, 'prazo': 'D+5'},
            'STANDARD': {'tarifa_kg': 3.90, 'minimo': 60.00, 'prazo': 'D+3'},
            'EXPRESSO': {'tarifa_kg': 5.60, 'minimo': 85.00, 'prazo': 'D+1'},
            'PREMIUM': {'tarifa_kg': 7.80, 'minimo': 120.00, 'prazo': 'próximo voo'},
        },
    },
    # Modalidade 4: cotação spot negociada no WhatsApp (não há tabela fixa)
    'Épsilon Rodoviário': {'modalidade': 'COTACAO_SPOT', 'modal': 'RODOVIARIO', 'fator_cubagem': 300},
}


# =============================================================================
# 3. CATÁLOGOS (listas fixas usadas pelas regras e pelos textos)
# =============================================================================
UFS = ['AC', 'AL', 'AP', 'AM', 'BA', 'CE', 'DF', 'ES', 'GO', 'MA', 'MT', 'MS', 'MG', 'PA', 'PB',
       'PR', 'PE', 'PI', 'RJ', 'RN', 'RS', 'RO', 'RR', 'SC', 'SP', 'SE', 'TO']
# Resolução do Senado nº 22/1989: saindo do Sul/Sudeste (exceto ES) para Norte, Nordeste,
# Centro-Oeste ou ES, a alíquota interestadual é 7%; nas demais operações interestaduais, 12%.
UF_ORIGEM_7 = ['SP', 'RJ', 'MG', 'PR', 'SC', 'RS']
UF_DESTINO_7 = ['AC', 'AM', 'AP', 'PA', 'RO', 'RR', 'TO', 'AL', 'BA', 'CE', 'MA', 'PB', 'PE', 'PI',
                'RN', 'SE', 'DF', 'GO', 'MS', 'MT', 'ES']

# Os três status possíveis de um CT-e auditado
STATUS_APROVADO = 'Aprovado'
STATUS_CRITICA = 'Divergência Crítica'
STATUS_ANALISE = 'Em Análise'

# Serviços do seletor, do mais barato para o mais caro (a ordem importa para achar "upgrade")
ORDEM_SERVICOS = ['ECOMMERCE', 'STANDARD', 'EXPRESSO', 'PREMIUM']
NOME_SERVICO = {'ECOMMERCE': 'E-commerce', 'STANDARD': 'Standard', 'EXPRESSO': 'Expresso', 'PREMIUM': 'Premium'}

# As modalidades de cálculo (nome completo para textos e nome curto para tabelas)
MODALIDADES = {
    'TABELA_ICMS': {'nome': 'Tabela combinada + ICMS', 'curto': 'Tabela + ICMS'},
    'CALCULADORA': {'nome': 'Motor de calculadora em massa', 'curto': 'Calculadora'},
    'SELETOR_SERVICO': {'nome': 'Seletor de serviço', 'curto': 'Seletor de serviço'},
    'COTACAO_SPOT': {'nome': 'Cotação spot (WhatsApp)', 'curto': 'Cotação spot'},
    'REFERENCIA_BI': {'nome': 'Sem regra cadastrada', 'curto': 'Referência BI'},
}

# Catálogo de achados: é o "enum" categoria_principal do JSON Schema do system prompt.
# sev = severidade ('critica' bloqueia e glosa; 'atencao' segura o pagamento para análise).
# A ORDEM das chaves é a prioridade de desempate quando dois achados empatam em valor.
GLOSAR = 'GLOSAR_E_NOTIFICAR_TRANSPORTADORA'
ACHADOS = {
    'DUPLICIDADE': {'rotulo': 'Cobrança em duplicidade', 'sev': 'critica', 'acao': GLOSAR, 'confianca': 0.99},
    'ALIQUOTA_ICMS_DIVERGENTE': {'rotulo': 'Alíquota de ICMS divergente', 'sev': 'critica', 'acao': 'SOLICITAR_CORRECAO_CTE', 'confianca': 0.95},
    'COBRANCA_ACIMA_FAIXA': {'rotulo': 'Cobrança acima da faixa de peso', 'sev': 'critica', 'acao': GLOSAR, 'confianca': 0.93},
    'PESO_CUBADO_DIVERGENTE': {'rotulo': 'Peso cubado divergente', 'sev': 'critica', 'acao': GLOSAR, 'confianca': 0.90},
    'AD_VALOREM_DIVERGENTE': {'rotulo': 'Ad valorem acima do contrato', 'sev': 'critica', 'acao': GLOSAR, 'confianca': 0.93},
    'GRIS_DIVERGENTE': {'rotulo': 'GRIS acima do contrato', 'sev': 'critica', 'acao': GLOSAR, 'confianca': 0.93},
    'SERVICO_SUPERIOR_FATURADO': {'rotulo': 'Serviço faturado acima do contratado', 'sev': 'critica', 'acao': GLOSAR, 'confianca': 0.88},
    'COTACAO_DIVERGENTE': {'rotulo': 'Cobrança acima da cotação acordada', 'sev': 'critica', 'acao': GLOSAR, 'confianca': 0.92},
    'TARIFA_ACIMA_TABELA': {'rotulo': 'Tarifa acima do contrato', 'sev': 'critica', 'acao': GLOSAR, 'confianca': 0.80},
    'NF_NAO_LOCALIZADA_BI': {'rotulo': 'NF não localizada no Power BI', 'sev': 'atencao', 'acao': 'VALIDAR_NF_BASE_INTERNA', 'confianca': 0.95},
    'COTACAO_AUSENTE': {'rotulo': 'Cotação spot não localizada', 'sev': 'atencao', 'acao': 'SOLICITAR_EVIDENCIA_COTACAO', 'confianca': 0.90},
    'ROTA_FORA_TABELA': {'rotulo': 'Rota fora da tabela contratual', 'sev': 'atencao', 'acao': 'VALIDAR_COM_SUPRIMENTOS', 'confianca': 0.92},
    'BI_DIVERGENTE_REGRA': {'rotulo': 'Previsto no Power BI diverge da regra', 'sev': 'atencao', 'acao': 'CORRIGIR_PREVISAO_BI', 'confianca': 0.88},
    'VALOR_ABAIXO_ESPERADO': {'rotulo': 'Cobrança abaixo do devido', 'sev': 'atencao', 'acao': 'MONITORAR_CTE_COMPLEMENTAR', 'confianca': 0.75},
}
ORDEM_ACHADOS = list(ACHADOS)   # dicionários do Python guardam a ordem em que foram escritos

# Texto de cada ação recomendada (o "enum" acao_recomendada do JSON Schema)
ACAO_LABEL = {
    'APROVAR_PAGAMENTO': 'Liberar pagamento integral',
    'GLOSAR_E_NOTIFICAR_TRANSPORTADORA': 'Glosar e notificar a transportadora',
    'SOLICITAR_CORRECAO_CTE': 'Solicitar CT-e de substituição',
    'VALIDAR_NF_BASE_INTERNA': 'Validar a NF na base interna',
    'VALIDAR_COM_SUPRIMENTOS': 'Validar rota com Suprimentos',
    'SOLICITAR_EVIDENCIA_COTACAO': 'Solicitar evidência da cotação',
    'CORRIGIR_PREVISAO_BI': 'Corrigir o previsto no SAP/BI e liberar',
    'MONITORAR_CTE_COMPLEMENTAR': 'Liberar e monitorar CT-e complementar',
}


class ErroEntrada(Exception):
    """Problema nas planilhas de entrada (arquivo inexistente, coluna faltando etc.)."""


class ErroLLM(Exception):
    """Problema na chamada ao LLM (sem chave, sem internet, resposta inválida etc.)."""


# =============================================================================
# 4. NÚMEROS E TEXTOS NO PADRÃO BRASILEIRO
# =============================================================================
EPSILON = sys.float_info.epsilon      # menor diferença entre dois floats perto de 1 (≈ 2,2e-16)


def arredondar(valor):
    """Arredonda para 2 casas, exatamente como o round2 do index.html.

    O round() do Python usa o "arredondamento do banqueiro" (2,5 vira 2), mas o JavaScript
    arredonda o meio para cima (2,5 vira 3). Para os dois lados darem o mesmo centavo,
    copiamos a fórmula do JavaScript: Math.round((n + EPSILON) * 100) / 100.
    """
    return math.floor((float(valor) + EPSILON) * 100 + 0.5) / 100


def limitar(valor, minimo, maximo):
    """Prende o valor dentro do intervalo [minimo, maximo]."""
    return min(max(valor, minimo), maximo)


def numero_valido(valor):
    """True se for um número de verdade (não None, não texto, não NaN, não infinito)."""
    return isinstance(valor, (int, float)) and not isinstance(valor, bool) and math.isfinite(valor)


def somar(valores):
    """Soma na mesma ordem e do mesmo jeito que o JavaScript e arredonda para centavos.

    A partir do Python 3.12, o sum() usa uma soma "compensada", que pode diferir da soma
    simples do JavaScript na última casa decimal. Um laço simples garante o mesmo centavo.
    """
    total = 0.0
    for valor in valores:                                   # percorre na ordem original
        total += valor if numero_valido(valor) else 0.0     # valores inválidos contam como zero
    return arredondar(total)


def _quantizar(valor, casas, exato=False):
    """Converte para Decimal com o número de casas pedido, arredondando o meio para cima.

    Detalhe sutil: o float 707.15 é guardado em binário como 707,1499999... Na tela, o JavaScript
    (Intl) arredonda pela forma decimal curta, "707.15", e mostra 707,2; é isso que repr() devolve.
    Já o toFixed() da exportação CSV usa o valor binário exato (707,1). Para as duas pontas
    baterem centavo a centavo, a tela usa repr() e o CSV usa exato=True.
    """
    if not numero_valido(valor) or valor == 0:   # None, NaN, infinito ou -0 viram 0
        valor = 0.0
    decimal = Decimal(valor) if exato else Decimal(repr(float(valor)))
    return decimal.quantize(Decimal(1).scaleb(-casas), rounding=ROUND_HALF_UP)


def numero_br(valor, casas=2):
    """1234.5 → '1.234,50' (milhar com ponto, decimal com vírgula)."""
    texto = f'{_quantizar(valor, casas):,.{casas}f}'        # primeiro no padrão americano: '1,234.50'
    # troca os separadores: vírgula → marcador, ponto → vírgula, marcador → ponto
    return texto.replace(',', '#').replace('.', ',').replace('#', '.')


def moeda(valor):
    """1234.5 → 'R$ 1.234,50'."""
    valor = valor if numero_valido(valor) else 0.0          # sem valor: mostra R$ 0,00
    sinal = '-' if valor < 0 else ''                        # negativo: '-R$ 5,00'
    return f'{sinal}R$ {numero_br(abs(valor))}'


def sinal_de(valor):
    """'+' para positivo, '−' (sinal de menos tipográfico) para negativo, '' para zero."""
    return '+' if valor > 0 else '−' if valor < 0 else ''


def moeda_sinal(valor):
    """12.3 → '+R$ 12,30'; -12.3 → '−R$ 12,30'."""
    valor = valor if numero_valido(valor) else 0.0
    return sinal_de(valor) + moeda(abs(valor))


def pct(valor, casas=1):
    """0.123 → '12,3%'."""
    valor = valor if numero_valido(valor) else 0.0
    return numero_br(valor * 100, casas) + '%'


def pct_sinal(valor):
    """0.123 → '+12,3%'."""
    valor = valor if numero_valido(valor) else 0.0
    return sinal_de(valor) + pct(abs(valor))


def kg(valor):
    """1234.56 → '1.234,6 kg'."""
    return numero_br(valor, 1) + ' kg'


def plural(quantidade, singular, forma_plural):
    """plural(1, 'CT-e', 'CT-es') → '1 CT-e'; plural(3, ...) → '3 CT-es'."""
    return f'{numero_br(quantidade, 0)} {singular if quantidade == 1 else forma_plural}'


def normalizar_texto(valor):
    """'  Épsilon Rodoviário ' → 'epsilon rodoviario' (sem acento, minúsculo, sem espaços nas pontas)."""
    texto = unicodedata.normalize('NFD', '' if valor is None else str(valor))  # 'É' vira 'E' + acento
    return re.sub('[\u0300-\u036f]', '', texto).lower().strip()               # apaga só os acentos


def normalizar_cabecalho(cabecalho):
    """'Valor Cobrado (Fatura)' → 'valor_cobrado_fatura': o nome de coluna vira um código estável."""
    codigo = re.sub(r'[^a-z0-9]+', '_', normalizar_texto(cabecalho))   # tudo que não é letra/número vira _
    return codigo.strip('_')                                           # tira os _ das pontas


def converter_numero(valor):
    """Aceita '1.234,56' (Brasil), '1234.56' (EUA), 'R$ 1.234,56' e '12%'. Inválido → None."""
    texto = re.sub(r'[R$\s%]', '', '' if valor is None else str(valor).strip())  # tira R, $, espaços e %
    if not texto:
        return None
    tem_virgula, tem_ponto = ',' in texto, '.' in texto
    if tem_virgula and tem_ponto:
        # quem aparece por último é o separador decimal
        if texto.rfind(',') > texto.rfind('.'):
            texto = texto.replace('.', '').replace(',', '.')   # 1.234,56 → 1234.56
        else:
            texto = texto.replace(',', '')                     # 1,234.56 → 1234.56
    elif tem_virgula:
        texto = texto.replace(',', '.', 1)                     # 12,5 → 12.5
    elif texto.count('.') > 1:
        texto = texto.replace('.', '')                         # 1.234.567 → 1234567 (milhar)
    try:
        numero = float(texto)
    except ValueError:                                         # sobrou algo que não é número
        return None
    return numero if math.isfinite(numero) else None


def limpar_numero(valor):
    """'NF 000184361' → '000184361': fica só com os dígitos (ou o texto original, se não houver dígito)."""
    texto = '' if valor is None else str(valor).strip()
    return re.sub(r'\D', '', texto) or texto


def chave_nf(nf):
    """Chave do cruzamento: só dígitos e sem zeros à esquerda ('000184361' e '184361' casam).

    Em produção, o ideal é cruzar pela chave de acesso da NF-e (44 dígitos), que é única no país.
    """
    texto = '' if nf is None else str(nf)
    return re.sub(r'\D', '', texto).lstrip('0') or texto.strip()


def normalizar_servico(valor):
    """'E-commerce', 'ECOMMERCE', 'expresso'... → código do serviço (ou '' se não reconhecer)."""
    s = re.sub(r'[^a-z]', '', normalizar_texto(valor))   # só letras: 'e-commerce' → 'ecommerce'
    if not s:
        return ''
    if 'ecommerce' in s:
        return 'ECOMMERCE'
    if 'standard' in s or 'padrao' in s:
        return 'STANDARD'
    if 'expresso' in s or 'express' in s:
        return 'EXPRESSO'
    if 'premium' in s:
        return 'PREMIUM'
    return ''


def normalizar_modalidade(valor):
    """Texto livre da coluna 'modalidade' → código da regra (ou '' se não reconhecer)."""
    s = normalizar_texto(valor)
    if not s:
        return ''
    if 'tabela' in s:
        return 'TABELA_ICMS'
    if 'calculadora' in s:
        return 'CALCULADORA'
    if 'seletor' in s or 'servico' in s:
        return 'SELETOR_SERVICO'
    if 'spot' in s or 'cotacao' in s or 'whatsapp' in s:
        return 'COTACAO_SPOT'
    if 'referencia' in s or 'sem regra' in s:
        return 'REFERENCIA_BI'
    return ''


def normalizar_bool(valor):
    """'SIM', 'S', 'x', '1'... → True; 'NÃO' → False; vazio → None (não informado)."""
    s = normalizar_texto(valor)
    if not s:
        return None
    return s in ('sim', 's', 'true', '1', 'yes', 'incluso', 'x')


def normalizar_uf(valor):
    """' go ' → 'GO'; UF inexistente → ''."""
    uf = ('' if valor is None else str(valor)).strip().upper()
    return uf if uf in UFS else ''


def normalizar_data(valor):
    """'25/10/2026' ou '2026-10-25' → '2026-10-25' (formato ISO). Outro formato → ''."""
    s = ('' if valor is None else str(valor)).strip()
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', s)
    if m:
        return f'{m.group(1)}-{m.group(2)}-{m.group(3)}'
    m = re.match(r'^(\d{2})/(\d{2})/(\d{4})$', s)
    return f'{m.group(3)}-{m.group(2)}-{m.group(1)}' if m else ''


def rota_txt(r):
    """'SP→GO', ou 'rota não informada' quando falta alguma UF."""
    return f"{r['uf_origem']}→{r['uf_destino']}" if r.get('uf_origem') and r.get('uf_destino') else 'rota não informada'


def inicial_minuscula(texto):
    """'Peso cubado divergente' → 'peso cubado divergente', mas 'GRIS acima' continua igual."""
    if re.match(r'^[A-ZÀ-Ú][a-zà-ú]', texto):     # maiúscula seguida de minúscula = palavra comum
        return texto[0].lower() + texto[1:]
    return texto


# =============================================================================
# 5. LEITURA DAS PLANILHAS COM PANDAS
# =============================================================================
# Cada campo interno aceita vários nomes de coluna ("aliases"), já normalizados. Assim a mesma
# função lê o modelo do painel, a planilha que a transportadora manda e a exportação do BI.
# A ordem importa: cada coluna da planilha é usada por um campo só, o primeiro que a reconhecer.
CAMPOS = [
    ('nf', ['nf', 'numero_nf', 'n_nf', 'no_nf', 'num_nf', 'nota_fiscal', 'nfe', 'nf_e']),
    ('numero_cte', ['numero_cte', 'cte', 'ct_e', 'n_cte', 'num_cte', 'numero_ct_e']),
    ('fatura', ['fatura', 'numero_fatura', 'n_fatura', 'num_fatura', 'id_fatura']),
    ('transportadora', ['transportadora', 'transp', 'carrier', 'razao_social_transportadora']),
    ('modalidade_informada', ['modalidade', 'regra_modalidade', 'regra']),
    ('uf_origem', ['uf_origem', 'origem']),
    ('uf_destino', ['uf_destino', 'destino']),
    ('data_emissao', ['data_emissao', 'data_emissao_ct_e', 'data_emissao_cte', 'emissao', 'data']),
    # Fonte 1: fatura da transportadora
    ('valor_cobrado', ['valor_cobrado', 'valor_cobrado_fatura', 'valor_fatura', 'valor_cte', 'valor_cobrado_ct_e',
                       'valor_ct_e', 'frete_cte', 'vl_cte', 'valor_prestacao']),
    ('peso_taxado_kg', ['peso_taxado_kg', 'peso_taxado', 'peso_taxado_fatura_kg', 'peso_taxado_cte_kg',
                        'peso_taxado_ct_e_kg', 'peso_cobrado', 'peso_cte']),
    ('frete_peso', ['frete_peso', 'frete_peso_fatura', 'frete_peso_cte', 'frete_peso_ct_e']),
    ('ad_valorem', ['ad_valorem', 'ad_valorem_fatura', 'ad_valorem_cte', 'ad_valorem_ct_e', 'frete_valor']),
    ('gris', ['gris', 'gris_fatura', 'gris_cte', 'gris_ct_e']),
    ('aliquota_icms', ['aliquota_icms', 'aliquota_icms_fatura', 'aliquota_icms_cte', 'aliquota_icms_ct_e', 'aliq_icms', 'icms']),
    ('servico_faturado', ['servico_faturado', 'servico_fatura', 'servico_cte', 'servico_ct_e', 'servico']),
    # Fonte 2: relatório do Power BI (base interna do SAP)
    ('peso_real_kg', ['peso_real_kg', 'peso_real', 'peso_real_bi_kg', 'peso_kg', 'peso_bruto']),
    ('volume_m3', ['volume_m3', 'volume', 'volume_bi_m3', 'cubagem_m3', 'm3']),
    ('valor_mercadoria', ['valor_mercadoria', 'valor_mercadoria_bi', 'valor_nf', 'vl_mercadoria']),
    ('frete_previsto_bi', ['frete_previsto_bi', 'frete_previsto', 'frete_previsto_power_bi', 'valor_previsto',
                           'valor_previsto_bi', 'previsto_bi', 'valor_frete_sap', 'valor_sap', 'valor_sap_esperado',
                           'frete_sap', 'valor_esperado']),
    ('servico_contratado', ['servico_contratado', 'servico_sap', 'servico_bi']),
    # Fonte 3 (parte variável): cotações spot registradas no WhatsApp
    ('cotacao_id', ['cotacao_id', 'id_cotacao', 'registro_cotacao']),
    ('cotacao_valor', ['valor_acordado', 'cotacao_valor', 'valor_cotacao']),
    ('cotacao_icms_incluso', ['icms_incluso', 'cotacao_icms_incluso']),
    ('cotacao_data', ['data_cotacao', 'cotacao_data']),
]

# O que dizer quando um arquivo não tem nenhuma linha aproveitável
MENSAGEM_SEM_LINHAS = {
    'fatura': 'nenhum CT-e válido. Confira NF, transportadora e valor cobrado.',
    'combinado': 'nenhum CT-e válido. Confira NF, transportadora e valor cobrado.',
    'bi': 'nenhuma nota válida. Confira NF e frete previsto.',
    'cotacoes': 'nenhuma cotação válida. Confira NF e valor acordado.',
}

# Como cada tipo de planilha é descrito nas mensagens para o usuário
DESCRICAO_PAPEL = {
    'fatura': 'a fatura da transportadora',
    'bi': 'o relatório do Power BI',
    'cotacoes': 'a planilha de cotações',
    'combinado': 'uma planilha combinada (fatura + Power BI)',
}


def ler_csv(caminho):
    """Lê o CSV como texto puro (todas as colunas como str), descobrindo codificação e separador."""
    arquivo = Path(caminho)
    if not arquivo.exists():
        raise ErroEntrada(f'Arquivo não encontrado: {caminho}')
    bruto = arquivo.read_bytes()                      # lê os bytes sem interpretar
    try:
        texto = bruto.decode('utf-8-sig')             # UTF-8, com ou sem BOM (o "utf-8-sig" remove o BOM)
    except UnicodeDecodeError:
        texto = bruto.decode('cp1252', errors='replace')  # o "CSV (separado por vírgulas)" do Excel em pt-BR
    primeira_linha = texto.split('\n', 1)[0]
    # O Excel brasileiro usa ";" e o americano usa ",": vence quem aparece mais no cabeçalho
    separador = ';' if primeira_linha.count(';') >= primeira_linha.count(',') else ','
    try:
        df = pd.read_csv(
            io.StringIO(texto),                       # o Pandas lê o texto como se fosse um arquivo
            sep=separador,
            dtype=str,                                # tudo como texto: preserva zeros à esquerda da NF
            keep_default_na=False,                    # célula vazia vira '' (e não NaN)
            skip_blank_lines=True,
        )
    except pd.errors.EmptyDataError:                  # arquivo vazio, sem nem o cabeçalho
        raise ErroEntrada(f'{caminho}: o arquivo está vazio.') from None
    except pd.errors.ParserError as erro:             # linhas com número de colunas diferente
        raise ErroEntrada(f'{caminho}: não foi possível ler o CSV ({erro}).') from None
    vazias = df.apply(lambda coluna: coluna.str.strip() == '').all(axis=1)   # linhas como ';;;;'
    return df[~vazias]                                # o ~ inverte: fica com as linhas que têm algum dado


def mapear_colunas(df):
    """Renomeia as colunas da planilha para os nomes internos (ex.: 'Valor Cobrado' → 'valor_cobrado')."""
    normalizados = [normalizar_cabecalho(c) for c in df.columns]
    usadas, renomear = set(), {}
    for chave, aliases in CAMPOS:                     # na ordem da lista CAMPOS
        for posicao, nome in enumerate(normalizados):
            if posicao not in usadas and nome in aliases:   # 1ª coluna livre que bate com um alias
                renomear[df.columns[posicao]] = chave
                usadas.add(posicao)
                break
    # Mantém só as colunas reconhecidas, já com o nome interno
    return df[list(renomear)].rename(columns=renomear)


def detectar_papel(colunas, caminho):
    """Descobre qual planilha é esta olhando as colunas (e não o nome do arquivo)."""
    if 'nf' not in colunas:
        raise ErroEntrada(f'{caminho}: coluna da NF não encontrada (nf, numero_nf ou nota_fiscal).')
    if 'valor_cobrado' in colunas and 'frete_previsto_bi' in colunas:
        return 'combinado'                            # ex.: a planilha padronizada exportada pelo painel
    if 'valor_cobrado' in colunas:
        return 'fatura'
    if 'frete_previsto_bi' in colunas:
        return 'bi'
    if 'cotacao_valor' in colunas:
        return 'cotacoes'
    raise ErroEntrada(f'{caminho}: planilha não reconhecida. Falta o valor cobrado (fatura), '
                      'o frete previsto (Power BI) ou o valor acordado (cotações).')


def texto(df, nome):
    """Devolve a coluna como texto sem espaços nas pontas; se não existir, uma coluna de ''."""
    if nome in df.columns:
        return df[nome].astype(str).str.strip()
    return pd.Series([''] * len(df), index=df.index, dtype=object)


def numero(df, nome):
    """Converte a coluna para número (float). Células inválidas viram NaN ("not a number")."""
    return texto(df, nome).map(converter_numero).astype(float)


def arredondar_coluna(serie):
    """Aplica arredondar() a cada valor válido da coluna (NaN continua NaN)."""
    return serie.map(lambda v: arredondar(v) if numero_valido(v) else v)


def ler_planilha(caminho):
    """Lê um CSV e separa o que ele contém: fatura, Power BI e/ou cotações."""
    df = mapear_colunas(ler_csv(caminho))
    if df.empty:
        raise ErroEntrada(f'{caminho}: o arquivo não tem linhas de dados abaixo do cabeçalho.')
    papel = detectar_papel(df.columns, caminho)
    le_fatura = papel in ('fatura', 'combinado')
    le_bi = papel in ('bi', 'combinado')
    if le_fatura and 'transportadora' not in df.columns:
        raise ErroEntrada(f'{caminho}: a fatura precisa da coluna transportadora.')

    nf = texto(df, 'nf').map(limpar_numero)           # a NF de cada linha, só com dígitos

    # ---- Fonte 1: fatura (cada coluna é convertida de uma vez, sem laço: é o jeito Pandas) ----
    fatura = pd.DataFrame({
        'nf': nf,
        'numero_cte': texto(df, 'numero_cte').map(limpar_numero),
        'fatura': texto(df, 'fatura'),
        'transportadora': texto(df, 'transportadora'),
        'modalidade_informada': texto(df, 'modalidade_informada').map(normalizar_modalidade),
        'uf_origem': texto(df, 'uf_origem').map(normalizar_uf),
        'uf_destino': texto(df, 'uf_destino').map(normalizar_uf),
        'data_emissao': texto(df, 'data_emissao').map(normalizar_data),
        'valor_cobrado': numero(df, 'valor_cobrado'),
        'peso_taxado_kg': numero(df, 'peso_taxado_kg').fillna(0.0),   # vazio conta como zero
        'frete_peso': numero(df, 'frete_peso').fillna(0.0),
        'ad_valorem': numero(df, 'ad_valorem').fillna(0.0),
        'gris': numero(df, 'gris').fillna(0.0),
        'aliquota_icms': numero(df, 'aliquota_icms'),     # sem fillna: vazio = "não informada"
        'servico_faturado': texto(df, 'servico_faturado').map(normalizar_servico),
    })
    # Uma linha de fatura só vale se tiver NF, transportadora e valor cobrado
    ok_fatura = (fatura['nf'] != '') & (fatura['transportadora'] != '') & fatura['valor_cobrado'].notna()
    fatura = fatura[ok_fatura] if le_fatura else fatura.iloc[0:0]   # iloc[0:0] = tabela vazia, mesmas colunas
    fatura['valor_cobrado'] = arredondar_coluna(fatura['valor_cobrado'])

    # ---- Fonte 2: relatório do Power BI ----
    servico = texto(df, 'servico_contratado')
    if papel == 'bi':
        # No relatório do BI, uma coluna chamada só "servico" significa o serviço contratado
        servico = servico.where(servico != '', texto(df, 'servico_faturado'))
    bi = pd.DataFrame({
        'nf': nf,
        'transportadora': texto(df, 'transportadora'),
        'modalidade_informada': texto(df, 'modalidade_informada').map(normalizar_modalidade),
        'uf_origem': texto(df, 'uf_origem').map(normalizar_uf),
        'uf_destino': texto(df, 'uf_destino').map(normalizar_uf),
        'peso_real_kg': numero(df, 'peso_real_kg').fillna(0.0),
        'volume_m3': numero(df, 'volume_m3').fillna(0.0),
        'valor_mercadoria': numero(df, 'valor_mercadoria').fillna(0.0),
        'frete_previsto_bi': numero(df, 'frete_previsto_bi'),
        'servico_contratado': servico.map(normalizar_servico),
    })
    ok_bi = (bi['nf'] != '') & bi['frete_previsto_bi'].notna()      # precisa de NF e frete previsto
    bi = bi[ok_bi] if le_bi else bi.iloc[0:0]
    bi['frete_previsto_bi'] = arredondar_coluna(bi['frete_previsto_bi'])

    # ---- Fonte 3: cotações (podem vir num arquivo próprio ou como colunas de outra planilha) ----
    cotacoes = pd.DataFrame({
        'nf': nf,
        'transportadora': texto(df, 'transportadora'),
        'cotacao_id': texto(df, 'cotacao_id'),
        'cotacao_valor': numero(df, 'cotacao_valor'),
        'cotacao_icms_incluso': texto(df, 'cotacao_icms_incluso').map(normalizar_bool),
        'cotacao_data': texto(df, 'cotacao_data'),
    })
    ok_cot = (cotacoes['nf'] != '') & (cotacoes['cotacao_valor'] > 0)   # NaN > 0 dá False
    cotacoes = cotacoes[ok_cot]
    cotacoes['cotacao_valor'] = arredondar_coluna(cotacoes['cotacao_valor'])

    # Quantas linhas do papel principal foram descartadas por falta de dado obrigatório
    validas = {'fatura': ok_fatura, 'combinado': ok_fatura, 'bi': ok_bi, 'cotacoes': ok_cot}[papel]
    rejeitadas = int((~validas).sum())
    total = {'fatura': len(fatura), 'combinado': len(fatura), 'bi': len(bi), 'cotacoes': len(cotacoes)}[papel]
    if total == 0:
        raise ErroEntrada(f'{caminho}: {MENSAGEM_SEM_LINHAS[papel]}')
    avisos = []
    if rejeitadas:
        avisos.append(f'{caminho}: {plural(rejeitadas, "linha ignorada", "linhas ignoradas")} por falta de dados obrigatórios.')
    if papel == 'combinado' and bi.empty:
        avisos.append(f'{caminho}: nenhuma linha tem frete previsto; as NFs ficarão sem dados do Power BI.')
    return {'papel': papel, 'fatura': fatura, 'bi': bi, 'cotacoes': cotacoes, 'avisos': avisos}


def carregar_fontes(caminho_fatura, caminho_bi=None, caminho_cotacoes=None):
    """Lê as três planilhas e confere se cada uma é mesmo o que o usuário disse que era."""
    avisos = []
    p_fatura = ler_planilha(caminho_fatura)
    if p_fatura['papel'] not in ('fatura', 'combinado'):
        raise ErroEntrada(f'{caminho_fatura}: este arquivo parece ser {DESCRICAO_PAPEL[p_fatura["papel"]]}, '
                          'não a fatura da transportadora.')
    avisos += p_fatura['avisos']
    fatura = p_fatura['fatura']
    # Planilha combinada já traz o Power BI junto; as cotações embutidas vêm com ele
    bi = p_fatura['bi'] if p_fatura['papel'] == 'combinado' else None
    cot_bi = p_fatura['cotacoes'] if p_fatura['papel'] == 'combinado' else None
    cot_fatura = None if p_fatura['papel'] == 'combinado' else p_fatura['cotacoes']

    if caminho_bi:                                    # um --power-bi explícito sempre vence
        p_bi = ler_planilha(caminho_bi)
        if p_bi['papel'] not in ('bi', 'combinado'):
            raise ErroEntrada(f'{caminho_bi}: este arquivo parece ser {DESCRICAO_PAPEL[p_bi["papel"]]}, '
                              'não o relatório do Power BI.')
        avisos += p_bi['avisos']
        bi, cot_bi = p_bi['bi'], p_bi['cotacoes']
    if bi is None:
        raise ErroEntrada('Falta o relatório do Power BI: use --power-bi (ou uma planilha combinada em --fatura).')

    cot_arquivo = None
    if caminho_cotacoes:
        p_cot = ler_planilha(caminho_cotacoes)
        if p_cot['papel'] != 'cotacoes':
            raise ErroEntrada(f'{caminho_cotacoes}: este arquivo parece ser {DESCRICAO_PAPEL[p_cot["papel"]]}, '
                              'não a planilha de cotações.')
        avisos += p_cot['avisos']
        cot_arquivo = p_cot['cotacoes']
    # Ordem de prioridade (igual ao painel): arquivo de cotações, depois o BI, depois a fatura
    cotacoes = pd.concat([c for c in (cot_arquivo, cot_bi, cot_fatura) if c is not None], ignore_index=True)
    return fatura, bi, cotacoes, avisos


def para_registros(df):
    """DataFrame → lista de dicionários, trocando NaN por None (o "vazio" do Python)."""
    registros = []
    for linha in df.to_dict('records'):               # cada linha vira {coluna: valor}
        registros.append({k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in linha.items()})
    return registros


# =============================================================================
# 6. CRUZAMENTO: FATURA × POWER BI × COTAÇÕES, PELO NÚMERO DA NF
# =============================================================================
def cruzar(fatura, bi, cotacoes):
    """Cada CT-e da fatura procura a sua nota no Power BI e a sua cotação (LEFT JOIN).

    LEFT JOIN quer dizer: todas as linhas da fatura ficam, mesmo as que não acharam nota no BI
    (essas viram o achado "NF não localizada no Power BI"). É o PROCV do Excel, mas para
    várias colunas de uma vez.
    """
    fat = fatura.reset_index(drop=True).copy()        # índice 0, 1, 2... na ordem da fatura
    fat['chave_nf'] = fat['nf'].map(chave_nf)

    bi = bi.copy()
    bi['chave_nf'] = bi['nf'].map(chave_nf)
    bi = bi[bi['chave_nf'] != '']
    bi_repetidas = int(bi['chave_nf'].duplicated().sum())          # NF que aparece 2 vezes no BI
    bi_unico = bi.drop_duplicates('chave_nf', keep='first')        # vale a primeira ocorrência

    cot = cotacoes.copy()
    cot['chave_nf'] = cot['nf'].map(chave_nf)
    cot_unica = cot[cot['chave_nf'] != ''].drop_duplicates('chave_nf', keep='first')

    # Colunas que vêm do BI, renomeando as que também existem na fatura para não colidirem
    colunas_bi = bi_unico[['chave_nf', 'uf_origem', 'uf_destino', 'peso_real_kg', 'volume_m3', 'valor_mercadoria',
                           'frete_previsto_bi', 'servico_contratado', 'modalidade_informada']].rename(
        columns={'uf_origem': 'uf_origem_bi', 'uf_destino': 'uf_destino_bi', 'modalidade_informada': 'modalidade_bi'})
    # how='left' mantém todas as linhas da fatura; indicator diz se a NF foi achada no BI
    base = fat.merge(colunas_bi, on='chave_nf', how='left', indicator='origem_bi')
    base['tem_bi'] = base['origem_bi'] == 'both'                   # 'both' = achou dos dois lados
    base = base.merge(cot_unica[['chave_nf', 'cotacao_id', 'cotacao_valor', 'cotacao_icms_incluso', 'cotacao_data']],
                      on='chave_nf', how='left')

    def primeiro_preenchido(principal, reserva):
        """Usa a coluna principal; onde ela estiver vazia, usa a reserva."""
        principal = principal.fillna('').astype(str)
        return principal.where(principal != '', reserva.fillna('').astype(str))

    base['uf_origem'] = primeiro_preenchido(base['uf_origem_bi'], base['uf_origem'])    # a rota vem do BI
    base['uf_destino'] = primeiro_preenchido(base['uf_destino_bi'], base['uf_destino'])
    base['modalidade_informada'] = primeiro_preenchido(base['modalidade_informada'], base['modalidade_bi'])
    for nome in ('peso_real_kg', 'volume_m3', 'valor_mercadoria'):
        base[nome] = base[nome].fillna(0.0)                         # sem BI: zero
    for nome in ('servico_contratado', 'cotacao_id', 'cotacao_data'):
        base[nome] = base[nome].fillna('').astype(str)
    base['id'] = [f'r{i + 1}' for i in range(len(base))]           # identificador de cada linha

    colunas = ['id', 'nf', 'numero_cte', 'fatura', 'transportadora', 'data_emissao', 'modalidade_informada',
               'valor_cobrado', 'peso_taxado_kg', 'frete_peso', 'ad_valorem', 'gris', 'aliquota_icms', 'servico_faturado',
               'tem_bi', 'uf_origem', 'uf_destino', 'peso_real_kg', 'volume_m3', 'valor_mercadoria', 'frete_previsto_bi',
               'servico_contratado', 'cotacao_id', 'cotacao_valor', 'cotacao_icms_incluso', 'cotacao_data']
    registros = para_registros(base[colunas])
    for r in registros:                                             # booleanos e "vazios" sem ambiguidade
        r['tem_bi'] = bool(r['tem_bi'])
        if not isinstance(r['cotacao_icms_incluso'], bool):
            r['cotacao_icms_incluso'] = None

    estatisticas = {
        'ctes': len(fat),
        'transportadoras': fat['transportadora'].map(normalizar_texto).nunique(),
        'cobrado': somar(fat['valor_cobrado'].tolist()),
        'notas_bi': len(bi_unico),
        'previsto': somar(bi_unico['frete_previsto_bi'].tolist()),
        'cruzados': int(base['tem_bi'].sum()),
        'sem_bi': int((~base['tem_bi']).sum()),
        'sem_cobranca': int((~bi_unico['chave_nf'].isin(fat['chave_nf'])).sum()),   # notas do BI sem CT-e
        'bi_repetidas': bi_repetidas,
        'cotacoes': len(cot_unica),
    }
    return registros, estatisticas


# =============================================================================
# 7. REGRAS FISCAIS E CÁLCULOS BÁSICOS (funções "puras": só calculam, não alteram nada)
# =============================================================================
def tolerancia(referencia):
    """Quanto de diferença aceitamos: R$ 1,00 ou 0,5% do valor de referência (o maior)."""
    return arredondar(max(TOLERANCIA_ABS, abs(referencia or 0) * TOLERANCIA_PCT))


def aliquota_interestadual(uf_origem, uf_destino, modal='RODOVIARIO'):
    """ICMS interestadual do frete. Devolve None quando a regra não se aplica.

    Rodoviário: Resolução do Senado 22/1989 (7% ou 12%). Aéreo: Resolução do Senado 95/1996 (4%).
    Operação dentro do mesmo estado (alíquota interna) ou UF ausente → None.
    """
    if uf_origem not in UFS or uf_destino not in UFS or uf_origem == uf_destino:
        return None
    if modal == 'AEREO':
        return 4
    return 7 if (uf_origem in UF_ORIGEM_7 and uf_destino in UF_DESTINO_7) else 12


def pesos(r, contrato):
    """Peso cubado (volume × fator do contrato) e peso taxável (o maior entre real e cubado)."""
    cubado = arredondar((r['volume_m3'] or 0) * contrato['fator_cubagem'])
    taxavel = arredondar(max(r['peso_real_kg'] or 0, cubado))
    return cubado, taxavel


def com_icms(base, aliquota):
    """Soma o ICMS "por dentro": o imposto faz parte da própria base (base ÷ (1 − alíquota))."""
    return arredondar(base / (1 - aliquota / 100))


def sem_icms(valor, aliquota):
    """Tira o ICMS "por dentro" de um valor que já o contém (valor × (1 − alíquota))."""
    return arredondar(valor * (1 - aliquota / 100))


def faixa_de_peso(contrato, peso):
    """Índice da faixa em que o peso se encaixa (0 = primeira). -1 = acima da última faixa."""
    for indice, limite in enumerate(contrato['faixas_kg']):
        if peso <= limite:
            return indice
    return -1


def rotulo_faixa(contrato, indice):
    """Índice 2 → '300–500 kg' (do limite anterior até o limite da faixa)."""
    inicio = 0 if indice == 0 else contrato['faixas_kg'][indice - 1]
    return f"{numero_br(inicio, 0)}–{numero_br(contrato['faixas_kg'][indice], 0)} kg"


def calcular_componentes(contrato, peso_taxavel, valor_nf):
    """Reproduz a calculadora da transportadora: frete-peso + ad valorem + GRIS (sem ICMS)."""
    frete_peso = arredondar(max(contrato['frete_peso_minimo'], peso_taxavel * contrato['tarifa_kg']))
    ad_valorem = arredondar((valor_nf * contrato['ad_valorem_pct']) / 100)
    gris = arredondar(max(contrato['gris_minimo'], (valor_nf * contrato['gris_pct']) / 100))
    return {'frete_peso': frete_peso, 'ad_valorem': ad_valorem, 'gris': gris,
            'subtotal': arredondar(frete_peso + ad_valorem + gris)}


def tarifa_servico(contrato, servico, peso_taxavel):
    """Valor de um serviço do seletor: peso × tarifa do serviço, respeitando o mínimo."""
    tabela = contrato['servicos'][servico]
    return arredondar(max(tabela['minimo'], peso_taxavel * tabela['tarifa_kg']))


# Índice dos contratos pelo nome sem acento: "EPSILON RODOVIARIO" acha "Épsilon Rodoviário"
INDICE_CONTRATOS = {normalizar_texto(nome): nome for nome in CONTRATOS}


def resolver_contrato(r):
    """Descobre qual contrato (e portanto qual regra) vale para esta transportadora."""
    nome = INDICE_CONTRATOS.get(normalizar_texto(r['transportadora']))
    if nome:
        return {'nome': nome, **CONTRATOS[nome]}           # ** copia todas as chaves do contrato
    # Cotação spot não depende de tabela: vale para qualquer transportadora com cotação registrada
    if r.get('modalidade_informada') == 'COTACAO_SPOT' or (numero_valido(r.get('cotacao_valor')) and r['cotacao_valor'] > 0):
        return {'nome': r['transportadora'], 'modalidade': 'COTACAO_SPOT', 'modal': 'RODOVIARIO', 'fator_cubagem': 300, 'generico': True}
    # Sem contrato: o frete previsto no Power BI vira a referência
    return {'nome': r['transportadora'], 'modalidade': 'REFERENCIA_BI', 'modal': 'RODOVIARIO', 'fator_cubagem': 300, 'generico': True}


def mem(item, regra, referencia='', fatura='', alerta=False, total=False):
    """Uma linha da memória de cálculo: o que foi calculado, como, o valor de referência e o da fatura."""
    return {'item': item, 'regra': regra, 'referencia': referencia, 'fatura': fatura, 'alerta': bool(alerta), 'total': total}


def achado(codigo, valor, detalhe):
    """Um achado do motor: código do catálogo, valor em R$ (0 se não gera glosa) e explicação."""
    return {'codigo': codigo, 'valor': valor, 'detalhe': detalhe}


def peso_acima(r, x):
    """A fatura taxou um peso mais de 5% acima do peso taxável correto?"""
    return r['peso_taxado_kg'] > x['taxavel'] * (1 + TOLERANCIA_PESO)


def linhas_peso(r, x, rotulo='Peso taxável'):
    """As três linhas de peso da memória de cálculo (real, cubado e taxável)."""
    return [
        mem('Peso real', 'Power BI (peso bruto da NF)', kg(r['peso_real_kg'])),
        mem('Peso cubado', f"{numero_br(r['volume_m3'], 3)} m³ do Power BI × {x['contrato']['fator_cubagem']} kg/m³", kg(x['cubado'])),
        mem(rotulo, 'maior entre real e cubado', kg(x['taxavel']),
            kg(r['peso_taxado_kg']) if r['peso_taxado_kg'] else '—', peso_acima(r, x)),
    ]


# =============================================================================
# 8. AS 4 MODALIDADES (cada uma responde duas perguntas)
#    calcular(): quanto deveria custar pela regra? (base sem ICMS + memória de cálculo)
#    atribuir(): se a fatura cobrou a mais, qual item da regra explica o excedente?
#    "x" é o contexto da nota: contrato, pesos, alíquotas e a base sem ICMS cobrada (base_cte).
# =============================================================================

# ---- Modalidade 1: tabela combinada + ICMS (rota × faixa de peso) ----
def calcular_tabela(r, x):
    contrato = x['contrato']
    tabela = contrato['tabela'].get(f"{r['uf_origem']}-{r['uf_destino']}")    # a linha da rota
    indice = faixa_de_peso(contrato, x['taxavel'])
    memoria = [mem('Rota', 'rota do Power BI encontrada na tabela contratual' if tabela else 'rota não consta na tabela contratual',
                   rota_txt(r), '', not tabela)] + linhas_peso(r, x)
    if not tabela or indice < 0:                         # sem rota ou peso acima da última faixa
        detalhe = (f'A rota {rota_txt(r)} não consta na tabela contratual da {contrato["nome"]}' if not tabela
                   else f'Peso taxável de {kg(x["taxavel"])} acima da última faixa contratada')
        return {'base': None, 'memoria': memoria, 'sem_base': {'codigo': 'ROTA_FORA_TABELA', 'detalhe': detalhe}}
    base = tabela[indice]
    # Qual faixa da tabela explica o valor cobrado? (procura o valor da tabela mais próximo da fatura)
    indice_cobrado = next((i for i, v in enumerate(tabela) if abs(v - x['base_cte']) <= tolerancia(v)), -1)
    faixa_correta = rotulo_faixa(contrato, indice)
    faixa_cobrada = rotulo_faixa(contrato, indice_cobrado) if indice_cobrado >= 0 else ''
    memoria.append(mem('Faixa de peso', f'enquadramento de {kg(x["taxavel"])}', faixa_correta,
                       faixa_cobrada or 'nenhuma faixa', indice_cobrado != indice))
    memoria.append(mem('Frete da faixa (sem ICMS)', f'tabela {contrato["nome"]}, rota {rota_txt(r)}', moeda(base),
                       moeda(x['base_cte']), x['base_cte'] - base > TOLERANCIA_COMPONENTE))
    return {'base': base, 'memoria': memoria,
            'extras': {'indice': indice, 'indice_cobrado': indice_cobrado, 'faixa_correta': faixa_correta, 'faixa_cobrada': faixa_cobrada}}


def atribuir_tabela(r, x, calc, excesso):
    e = calc['extras']
    if not e['indice_cobrado'] > e['indice']:            # só é "faixa acima" se cobrou uma faixa maior
        return []
    declarado = f' com peso taxado de {kg(r["peso_taxado_kg"])}' if peso_acima(r, x) else ''
    return [{'codigo': 'COBRANCA_ACIMA_FAIXA', 'valor': excesso,
             'detalhe': f'A fatura cobrou a faixa {e["faixa_cobrada"]}{declarado}; o peso taxável de {kg(x["taxavel"])} '
                        f'enquadra na faixa {e["faixa_correta"]}'}]


# ---- Modalidade 2: calculadora em massa (frete-peso + ad valorem + GRIS, componente a componente) ----
def calcular_calculadora(r, x):
    contrato = x['contrato']
    v = calcular_componentes(contrato, x['taxavel'], r['valor_mercadoria'])
    tem_componentes = (r['frete_peso'] or 0) + (r['ad_valorem'] or 0) + (r['gris'] or 0) > 0

    def na_fatura(valor):                                # sem componentes na fatura, mostra "—"
        return moeda(valor or 0) if tem_componentes else '—'

    def acima(valor, referencia):                        # componente cobrado acima do contrato?
        return tem_componentes and (valor or 0) - referencia > TOLERANCIA_COMPONENTE

    memoria = linhas_peso(r, x, 'Peso taxado') + [
        mem('Frete-peso', f"{kg(x['taxavel'])} × {moeda(contrato['tarifa_kg'])}/kg, mínimo {moeda(contrato['frete_peso_minimo'])}",
            moeda(v['frete_peso']), na_fatura(r['frete_peso']), acima(r['frete_peso'], v['frete_peso'])),
        mem('Ad valorem', f"{numero_br(contrato['ad_valorem_pct'], 2)}% × mercadoria de {moeda(r['valor_mercadoria'])} (Power BI)",
            moeda(v['ad_valorem']), na_fatura(r['ad_valorem']), acima(r['ad_valorem'], v['ad_valorem'])),
        mem('GRIS', f"{numero_br(contrato['gris_pct'], 2)}% × mercadoria, mínimo {moeda(contrato['gris_minimo'])}",
            moeda(v['gris']), na_fatura(r['gris']), acima(r['gris'], v['gris'])),
        mem('Subtotal sem ICMS', 'frete-peso + ad valorem + GRIS', moeda(v['subtotal']), moeda(x['base_cte']),
            x['base_cte'] - v['subtotal'] > TOLERANCIA_COMPONENTE),
    ]
    return {'base': v['subtotal'], 'memoria': memoria, 'extras': {'v': v, 'tem_componentes': tem_componentes}}


def atribuir_calculadora(r, x, calc, excesso):
    contrato = x['contrato']
    v, tem_componentes = calc['extras']['v'], calc['extras']['tem_componentes']
    saida = []
    if tem_componentes:
        # Excedentes vêm SEM ICMS ("valor_base"); o motor soma o ICMS depois
        excesso_fp = arredondar((r['frete_peso'] or 0) - v['frete_peso'])
        if excesso_fp > TOLERANCIA_COMPONENTE and peso_acima(r, x):
            saida.append({'codigo': 'PESO_CUBADO_DIVERGENTE', 'valor_base': excesso_fp,
                          'detalhe': f"A fatura taxou {kg(r['peso_taxado_kg'])}; o correto é {kg(x['taxavel'])} "
                                     f"(maior entre real de {kg(r['peso_real_kg'])} e cubado de {kg(x['cubado'])})"})
        excesso_av = arredondar((r['ad_valorem'] or 0) - v['ad_valorem'])
        pct_cobrado = (r['ad_valorem'] / r['valor_mercadoria']) * 100 if r['valor_mercadoria'] else 0
        if excesso_av > TOLERANCIA_COMPONENTE:
            saida.append({'codigo': 'AD_VALOREM_DIVERGENTE', 'valor_base': excesso_av,
                          'detalhe': f"A fatura aplicou {numero_br(pct_cobrado, 2)}% sobre a mercadoria; "
                                     f"o contrato prevê {numero_br(contrato['ad_valorem_pct'], 2)}%"})
        excesso_gr = arredondar((r['gris'] or 0) - v['gris'])
        if excesso_gr > TOLERANCIA_COMPONENTE:
            saida.append({'codigo': 'GRIS_DIVERGENTE', 'valor_base': excesso_gr,
                          'detalhe': f"A fatura cobrou GRIS de {moeda(r['gris'])}; o contrato prevê "
                                     f"{numero_br(contrato['gris_pct'], 2)}% da mercadoria ({moeda(v['gris'])})"})
    elif peso_acima(r, x):
        # Fatura sem componentes: estima o efeito do peso pela tarifa por kg
        saida.append({'codigo': 'PESO_CUBADO_DIVERGENTE',
                      'valor_base': arredondar((r['peso_taxado_kg'] - x['taxavel']) * contrato['tarifa_kg']),
                      'detalhe': f"A fatura taxou {kg(r['peso_taxado_kg'])}; o correto é {kg(x['taxavel'])} "
                                 f"(efeito estimado pela tarifa de {moeda(contrato['tarifa_kg'])}/kg)"})
    return saida


def residuo_calculadora(r, x, calc):
    v, tem_componentes = calc['extras']['v'], calc['extras']['tem_componentes']
    if tem_componentes and (r['frete_peso'] or 0) - v['frete_peso'] > TOLERANCIA_COMPONENTE:
        return f"Frete-peso de {moeda(r['frete_peso'])} acima do contratado ({moeda(v['frete_peso'])}) sem divergência de peso"
    return None


# ---- Modalidade 3: seletor de serviço (qual tarifa explica a fatura? ela é a contratada?) ----
def calcular_seletor(r, x):
    contrato = x['contrato']
    contratado = r['servico_contratado'] if r['servico_contratado'] in contrato['servicos'] else 'STANDARD'
    valores = [(s, tarifa_servico(contrato, s, x['taxavel'])) for s in ORDEM_SERVICOS]
    # "Melhor adequação": a tarifa cujo valor fica mais perto da base cobrada na fatura
    inferido = valores[0]
    for candidato in valores[1:]:
        if abs(candidato[1] - x['base_cte']) < abs(inferido[1] - x['base_cte']):
            inferido = candidato
    inferido = inferido[0]
    upgrade = ORDEM_SERVICOS.index(inferido) > ORDEM_SERVICOS.index(contratado)   # cobrou um serviço mais caro?
    base = dict(valores)[contratado]
    memoria = [mem('Serviço contratado', 'Power BI (ordem de frete)' if r['servico_contratado'] else 'não informado no Power BI; assumido Standard',
                   NOME_SERVICO[contratado],
                   f"{NOME_SERVICO[r['servico_faturado']]} (faturado)" if r['servico_faturado'] else 'não declarado',
                   bool(r['servico_faturado']) and r['servico_faturado'] != contratado)]
    memoria += linhas_peso(r, x, 'Peso taxável aéreo')
    for s, valor in valores:
        tab = contrato['servicos'][s]
        memoria.append(mem(f"Tarifa {NOME_SERVICO[s]}{' (contratada)' if s == contratado else ''}",
                           f"{kg(x['taxavel'])} × {moeda(tab['tarifa_kg'])}/kg, mínimo {moeda(tab['minimo'])}, prazo {tab['prazo']}",
                           moeda(valor), 'melhor adequação' if s == inferido else '', s == inferido and upgrade))
    memoria.append(mem('Base cobrada (sem ICMS)', f'comparada à tarifa {NOME_SERVICO[contratado]}', moeda(base),
                       moeda(x['base_cte']), x['base_cte'] - base > TOLERANCIA_COMPONENTE))
    return {'base': base, 'memoria': memoria, 'extras': {'contratado': contratado, 'inferido': inferido, 'upgrade': upgrade}}


def atribuir_seletor(r, x, calc, excesso):
    e = calc['extras']
    if not e['upgrade']:
        return []
    declarado = f"; a fatura declara {NOME_SERVICO[r['servico_faturado']]}" if r['servico_faturado'] else ''
    return [{'codigo': 'SERVICO_SUPERIOR_FATURADO', 'valor': excesso,
             'detalhe': f"O valor cobrado corresponde à tarifa {NOME_SERVICO[e['inferido']]}, mas o contratado é "
                        f"{NOME_SERVICO[e['contratado']]}{declarado}"}]


# ---- Modalidade 4: cotação spot (valor acordado no WhatsApp × Power BI × fatura) ----
def calcular_cotacao(r, x):
    tem = numero_valido(r['cotacao_valor']) and r['cotacao_valor'] > 0
    memoria = [mem('Registro da cotação',
                   f"{r['cotacao_id'] or 'sem ID'} · WhatsApp{' · ' + r['cotacao_data'] if r['cotacao_data'] else ''}" if tem else 'nenhum registro localizado',
                   moeda(r['cotacao_valor']) if tem else '—', '', not tem)]
    if not tem:
        memoria.append(mem('Previsto no Power BI', 'sem cotação que comprove o valor', moeda(r['frete_previsto_bi'])))
        return {'base': None, 'memoria': memoria,
                'sem_base': {'codigo': 'COTACAO_AUSENTE', 'detalhe': 'Nenhuma cotação spot (WhatsApp) registrada para esta NF'}}
    incluso = r['cotacao_icms_incluso'] is not False          # não informado conta como incluso
    devido = arredondar(r['cotacao_valor']) if incluso else com_icms(r['cotacao_valor'], x['aliq_calc'])
    if incluso:
        regra_icms = 'condição não informada; assumido ICMS incluso' if r['cotacao_icms_incluso'] is None else 'ICMS incluso no valor acordado'
    else:
        regra_icms = f"ICMS por fora: {moeda(r['cotacao_valor'])} ÷ (1 − {numero_br(x['aliq_calc'] / 100, 2)})"
    memoria.append(mem(f"Condição de ICMS ({numero_br(x['aliq_calc'], 0)}%)", regra_icms, moeda(devido),
                       f"{moeda(r['valor_cobrado'])} ({numero_br(x['aliq_cte'], 0)}%)", r['valor_cobrado'] - devido > tolerancia(devido)))
    bi_confere = abs(r['frete_previsto_bi'] - devido) <= tolerancia(devido)
    memoria.append(mem('Previsto no Power BI', 'confere com a cotação' if bi_confere else 'diverge da cotação',
                       moeda(r['frete_previsto_bi']), '', not bi_confere))
    # total_fixo: com ICMS incluso, o total acordado não muda mesmo que a alíquota da fatura esteja errada
    return {'base': sem_icms(r['cotacao_valor'], x['aliq_calc']) if incluso else r['cotacao_valor'],
            'devido': devido, 'total_fixo': incluso, 'linha_icms': False, 'memoria': memoria, 'extras': {'incluso': incluso}}


def atribuir_cotacao(r, x, calc, excesso):
    # A transportadora somou ICMS por fora numa cotação fechada "com ICMS incluso"?
    por_fora = calc['extras']['incluso'] and abs(x['base_cte'] - r['cotacao_valor']) <= tolerancia(r['cotacao_valor'])
    if por_fora:
        detalhe = (f"A transportadora somou ICMS por fora sobre a cotação {r['cotacao_id']}, "
                   f"fechada com ICMS incluso em {moeda(r['cotacao_valor'])}")
    else:
        detalhe = f"Fatura acima do valor acordado na cotação {r['cotacao_id']} ({moeda(calc['devido'])})"
    return [{'codigo': 'COTACAO_DIVERGENTE', 'valor': excesso, 'detalhe': detalhe}]


# ---- Sem contrato cadastrado: o frete previsto no Power BI vira a referência ----
def calcular_referencia(r, x):
    memoria = [mem('Sem regra cadastrada', f"{x['contrato']['nome']} não tem contrato no motor; referência = frete previsto no Power BI",
                   moeda(r['frete_previsto_bi']))]
    return {'base': sem_icms(r['frete_previsto_bi'], x['aliq_calc']), 'devido': r['frete_previsto_bi'],
            'total_fixo': True, 'linha_icms': False, 'memoria': memoria}


def atribuir_referencia(r, x, calc, excesso):
    return []


def residuo_referencia(r, x, calc):
    return f"Fatura acima do frete previsto no Power BI ({moeda(r['frete_previsto_bi'])}) e sem regra contratual cadastrada"


# Dicionário que liga cada modalidade às suas funções. Para criar uma 5ª regra, basta escrever as
# funções e acrescentar uma linha aqui (é o padrão de projeto "Strategy").
MOTOR = {
    'TABELA_ICMS': {'calcular': calcular_tabela, 'atribuir': atribuir_tabela, 'residuo': None},
    'CALCULADORA': {'calcular': calcular_calculadora, 'atribuir': atribuir_calculadora, 'residuo': residuo_calculadora},
    'SELETOR_SERVICO': {'calcular': calcular_seletor, 'atribuir': atribuir_seletor, 'residuo': None},
    'COTACAO_SPOT': {'calcular': calcular_cotacao, 'atribuir': atribuir_cotacao, 'residuo': None},
    'REFERENCIA_BI': {'calcular': calcular_referencia, 'atribuir': atribuir_referencia, 'residuo': residuo_referencia},
}


# =============================================================================
# 9. MOTOR DE AUDITORIA (determinístico: os números vêm daqui, nunca do LLM)
# =============================================================================
def chave_cte(r):
    """Identidade do CT-e para achar duplicidade: transportadora + número (sem zeros à esquerda)."""
    if not r['numero_cte']:
        return None
    return normalizar_texto(r['transportadora']) + '|' + str(r['numero_cte']).lstrip('0')


def auditar(r, vistos):
    """Audita UM CT-e já cruzado e devolve o resultado completo.

    "vistos" guarda os CT-es já processados nesta fatura, para detectar cobrança em duplicidade.
    Ordem das verificações: duplicidade → NF no Power BI → regra da modalidade → ICMS →
    excedente por causa → resíduo sem causa → Power BI × regra.
    """
    contrato = resolver_contrato(r)
    regra = MOTOR[contrato['modalidade']]
    cubado, taxavel = pesos(r, contrato)
    aliq_esp = aliquota_interestadual(r['uf_origem'], r['uf_destino'], contrato['modal'])   # o que a lei manda
    if numero_valido(r['aliquota_icms']):
        aliq_cte = r['aliquota_icms']                     # o que a transportadora destacou
    else:
        aliq_cte = aliq_esp if aliq_esp is not None else 0
    aliq_calc = aliq_esp if aliq_esp is not None else aliq_cte   # a alíquota usada no recálculo
    base_cte = sem_icms(r['valor_cobrado'], aliq_cte)             # quanto a fatura cobrou sem ICMS
    x = {'contrato': contrato, 'cubado': cubado, 'taxavel': taxavel, 'aliq_esp': aliq_esp,
         'aliq_cte': aliq_cte, 'aliq_calc': aliq_calc, 'base_cte': base_cte}
    tem_bi = bool(r['tem_bi']) and numero_valido(r['frete_previsto_bi'])
    diferenca = arredondar(r['valor_cobrado'] - r['frete_previsto_bi']) if tem_bi else None
    achados, memoria, devido, extras = [], [], None, {}

    chave = chave_cte(r)
    nf_original = vistos.get(chave) if chave else None
    if nf_original is not None:
        # R1 · Duplicidade: aparece na chave do documento, não na diferença de valor
        achados.append(achado('DUPLICIDADE', r['valor_cobrado'],
                              f"CT-e {r['numero_cte']} já foi cobrado nesta fatura para a NF {nf_original}"))
        devido = 0
        extras = {'nf_original': nf_original}
        memoria = [mem('CT-e já cobrado', f'mesmo CT-e lançado para a NF {nf_original}', moeda(0),
                       moeda(r['valor_cobrado']), True, True)]
    else:
        if chave:
            vistos[chave] = r['nf']                       # registra este CT-e como já visto
        if not tem_bi:
            # R2 · Cruzamento: sem a nota no Power BI não há peso, rota nem mercadoria para recalcular
            achados.append(achado('NF_NAO_LOCALIZADA_BI', 0, f"A NF {r['nf']} cobrada na fatura não consta no relatório do Power BI"))
            memoria = [mem('NF no Power BI', 'cruzamento pelo número da NF', 'não localizada', f"NF {r['nf']}", True),
                       mem('Valor da prestação', 'sem peso, rota e mercadoria para recalcular', '—',
                           moeda(r['valor_cobrado']), True, True)]
        else:
            # R3 · Regra da modalidade: quanto deveria custar?
            calc = regra['calcular'](r, x)
            memoria = calc['memoria']
            extras = calc.get('extras', {})
            # R4 · ICMS: alíquota destacada na fatura × alíquota interestadual esperada
            efeito_icms = 0
            if aliq_esp is not None and numero_valido(r['aliquota_icms']) and abs(r['aliquota_icms'] - aliq_esp) > 0.001:
                efeito_icms = 0 if calc.get('total_fixo') else arredondar(r['valor_cobrado'] - com_icms(base_cte, aliq_esp))
                modal_txt = 'aérea ' if contrato['modal'] == 'AEREO' else ''
                achados.append(achado('ALIQUOTA_ICMS_DIVERGENTE', max(efeito_icms, 0),
                                      f"A fatura destacou ICMS de {numero_br(r['aliquota_icms'], 0)}%; "
                                      f"a prestação {modal_txt}{rota_txt(r)} exige {aliq_esp}%"))
            if calc['base'] is None:
                # Sem base contratual (rota fora da tabela ou cotação ausente): segura para análise
                achados.append(achado(calc['sem_base']['codigo'], 0, calc['sem_base']['detalhe']))
            else:
                devido = calc['devido'] if calc.get('devido') is not None else com_icms(calc['base'], aliq_calc)
                tol = tolerancia(devido)
                # R5 · Excedente: o que a fatura cobrou acima da regra, já sem o efeito do ICMS
                restante = arredondar(r['valor_cobrado'] - efeito_icms - devido)
                for item in regra['atribuir'](r, x, calc, restante):
                    # componente sem ICMS ("valor_base") ganha o ICMS por dentro; os demais já vêm com ICMS
                    bruto = item['valor'] if item.get('valor') is not None else item['valor_base'] / (1 - aliq_calc / 100)
                    valor = arredondar(limitar(bruto, 0, max(restante, 0)))   # nunca explica mais do que sobrou
                    if valor > 0:
                        achados.append(achado(item['codigo'], valor, item['detalhe']))
                        restante = arredondar(restante - valor)
                if restante > tol:
                    # R6 · Resíduo: sobrou excedente sem causa específica
                    detalhe = (regra['residuo'](r, x, calc) if regra['residuo'] else None) or \
                        f"Excedente de {moeda(restante)} sem causa identificada na regra {MODALIDADES[contrato['modalidade']]['curto']}"
                    achados.append(achado('TARIFA_ACIMA_TABELA', restante, detalhe))
                elif restante < -tol:
                    achados.append(achado('VALOR_ABAIXO_ESPERADO', 0, f'Fatura {moeda(-restante)} abaixo do devido pela regra'))
                # R7 · Power BI × regra: a previsão interna bate com o que o contrato manda pagar?
                if abs(r['frete_previsto_bi'] - devido) > tol:
                    referencia = 'a cotação' if contrato['modalidade'] == 'COTACAO_SPOT' else 'a regra'
                    achados.append(achado('BI_DIVERGENTE_REGRA', 0,
                                          f"Power BI com {moeda(r['frete_previsto_bi'])}; {referencia} resulta em {moeda(devido)}"))
            if calc['base'] is not None and calc.get('linha_icms', True):
                regra_icms = ('operação interna: alíquota da fatura aceita' if aliq_esp is None
                              else f"{moeda(calc['base'])} ÷ (1 − {numero_br(aliq_calc / 100, 2)})")
                memoria.append(mem(f'ICMS por dentro ({numero_br(aliq_calc, 0)}%)', regra_icms, moeda(devido),
                                   f"{moeda(r['valor_cobrado'])} ({numero_br(aliq_cte, 0)}%)", efeito_icms > 0))
            memoria.append(mem('Valor da prestação',
                               'sem base contratual para recalcular' if devido is None else 'devido pela regra × cobrado na fatura',
                               '—' if devido is None else moeda(devido), moeda(r['valor_cobrado']),
                               devido is None or r['valor_cobrado'] - devido > tolerancia(devido), True))

    # Decisão: crítica > atenção > aprovado
    criticos = [a for a in achados if ACHADOS[a['codigo']]['sev'] == 'critica']
    atencao = [a for a in achados if ACHADOS[a['codigo']]['sev'] == 'atencao']
    status = STATUS_CRITICA if criticos else STATUS_ANALISE if atencao else STATUS_APROVADO
    if criticos:   # o crítico de maior valor é o principal; empate → ordem do catálogo
        principal = sorted(criticos, key=lambda a: (-a['valor'], ORDEM_ACHADOS.index(a['codigo'])))[0]
    elif atencao:
        principal = sorted(atencao, key=lambda a: ORDEM_ACHADOS.index(a['codigo']))[0]
    else:
        principal = None
    adicionais = len(criticos) + len(atencao) - 1
    categoria = principal['codigo'] if principal else 'APROVADO'
    diagnostico = (ACHADOS[categoria]['rotulo'] + (f' (+{adicionais})' if adicionais > 0 else '')) if principal else 'Aprovado'
    glosa = arredondar(sum(a['valor'] for a in criticos))   # só achados críticos viram glosa

    return {
        **r,                                                 # todos os dados de entrada
        'modalidade': contrato['modalidade'], 'modal': contrato['modal'], 'contrato_generico': bool(contrato.get('generico')),
        'tem_bi': tem_bi, 'diferenca': diferenca,
        'diferenca_pct': diferenca / r['frete_previsto_bi'] if tem_bi and r['frete_previsto_bi'] > 0 else None,
        'tolerancia': tolerancia(devido if devido is not None else r['frete_previsto_bi'] if r['frete_previsto_bi'] is not None else r['valor_cobrado']),
        'devido': devido, 'excedente': None if devido is None else arredondar(r['valor_cobrado'] - devido),
        'peso_cubado_kg': cubado, 'peso_taxavel_kg': taxavel, 'aliquota_icms_esperada': aliq_esp,
        'servico_inferido': extras.get('inferido', ''), 'faixa_correta': extras.get('faixa_correta', ''),
        'faixa_cobrada': extras.get('faixa_cobrada', ''), 'nf_original': extras.get('nf_original', ''),
        'memoria': memoria, 'achados': achados, 'status': status, 'categoria_principal': categoria,
        'diagnostico': diagnostico, 'glosa': glosa, 'valor_a_pagar': arredondar(max(r['valor_cobrado'] - glosa, 0)),
        'acao_recomendada': ACHADOS[categoria]['acao'] if principal else 'APROVAR_PAGAMENTO',
        'requer_revisao_humana': (status == STATUS_ANALISE or glosa >= ALCADA_REVISAO_HUMANA or categoria == 'TARIFA_ACIMA_TABELA'
                                  or len(criticos) > 1 or bool(contrato.get('generico'))),
        'auditado_em': datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
    }


def auditar_base(registros):
    """Audita todos os CT-es na ordem da fatura (a ordem importa para achar a duplicidade)."""
    vistos = {}
    return [auditar(r, vistos) for r in registros]


# =============================================================================
# 10. DEFESA TÉCNICA PARA O FINANCEIRO (o texto que vai no e-mail de glosa)
# =============================================================================
def titulo_defesa(l):
    """O título muda conforme a decisão: glosa, bloqueio ou liberação."""
    if l['status'] == STATUS_CRITICA:
        return 'Defesa técnica de glosa'
    if l['status'] == STATUS_ANALISE:
        return 'Justificativa de bloqueio preventivo'
    return 'Parecer de conformidade para pagamento'


def documento(l):
    """'O CT-e 55900 (NF 184302, Gama Logística, fatura FT-GAM-1026-2)': o sujeito das frases."""
    fatura = f", fatura {l['fatura']}" if l['fatura'] else ''
    return f"O CT-e {l['numero_cte'] or 's/nº'} (NF {l['nf']}, {l['transportadora']}{fatura})"


def fato_apurado(l):
    """Seção 1 da defesa: o que aconteceu, em uma ou duas frases."""
    doc = documento(l)
    codigos = [a['codigo'] for a in l['achados']]
    if 'DUPLICIDADE' in codigos:
        return (f"{doc} já foi cobrado para a NF {l['nf_original']}. A nova cobrança de {moeda(l['valor_cobrado'])} "
                'é duplicada e não tem causa operacional.')
    if 'NF_NAO_LOCALIZADA_BI' in codigos:
        return (f"{doc} cobra {moeda(l['valor_cobrado'])}, mas a NF não consta no relatório do Power BI. Sem a nota na "
                'base interna não há peso, rota nem valor de mercadoria para validar o frete.')
    if l['devido'] is None:
        if 'COTACAO_AUSENTE' in codigos:
            return (f"{doc} foi faturado em {moeda(l['valor_cobrado'])} como frete spot, mas não há registro da cotação "
                    '(WhatsApp) que comprove o valor acordado.')
        return (f"{doc} foi faturado em {moeda(l['valor_cobrado'])} na rota {rota_txt(l)}, que não consta na tabela "
                'contratual. Sem base contratual, o valor não pode ser validado.')
    base = (f"{doc} foi faturado em {moeda(l['valor_cobrado'])}; pela regra \"{MODALIDADES[l['modalidade']]['nome']}\", "
            f"o valor devido é {moeda(l['devido'])}")
    criticos = [a for a in l['achados'] if ACHADOS[a['codigo']]['sev'] == 'critica']
    atencao = [a for a in l['achados'] if ACHADOS[a['codigo']]['sev'] == 'atencao']
    if l['glosa'] > 0:
        causas = ', '.join(inicial_minuscula(ACHADOS[a['codigo']]['rotulo']) for a in criticos)
        return f"{base}. A diferença de {moeda(l['glosa'])} não tem respaldo contratual: {causas}."
    if criticos:                                         # erro fiscal sem efeito no valor
        return f"{base}, mas o documento tem erro fiscal: {inicial_minuscula(criticos[0]['detalhe'])}."
    if atencao:
        return f"{base}, porém há pendência que impede a liberação: {inicial_minuscula(atencao[0]['detalhe'])}."
    return f"{base}, dentro da tolerância de {moeda(l['tolerancia'])}, e confere com o frete previsto no Power BI."


def encaminhamento(l):
    """Seção 5 da defesa: o que o Financeiro deve fazer com este CT-e."""
    t = l['transportadora']
    acao = l['acao_recomendada']
    if acao == 'GLOSAR_E_NOTIFICAR_TRANSPORTADORA':
        if l['categoria_principal'] == 'DUPLICIDADE':
            return f"Glosar o valor integral de {moeda(l['glosa'])} e solicitar à {t} o cancelamento da cobrança duplicada."
        return (f"Glosar {moeda(l['glosa'])} e pagar {moeda(l['valor_a_pagar'])}. Notificar a {t} para emitir CT-e de "
                'substituição com o valor devido, anexando esta memória de cálculo.')
    if acao == 'SOLICITAR_CORRECAO_CTE':
        glosa = f" Glosar {moeda(l['glosa'])} até a regularização." if l['glosa'] > 0 else ''
        return (f"Solicitar à {t} CT-e de substituição com a alíquota de {l['aliquota_icms_esperada']}% "
                f"(alíquota não se corrige por carta de correção).{glosa}")
    if acao == 'VALIDAR_NF_BASE_INTERNA':
        return (f"Bloquear o pagamento de {moeda(l['valor_cobrado'])} e validar com a Logística se a NF {l['nf']} pertence à "
                f"empresa e ao período. Se a nota não for reconhecida, glosar o valor integral e devolver o CT-e à {t}.")
    if acao == 'VALIDAR_COM_SUPRIMENTOS':
        return f"Bloquear o pagamento e acionar Suprimentos para formalizar a rota {rota_txt(l)} (cotação formal ou aditivo à tabela)."
    if acao == 'SOLICITAR_EVIDENCIA_COTACAO':
        return ('Bloquear o pagamento até anexar a evidência da negociação (print do WhatsApp ou e-mail de confirmação) '
                'com o valor acordado.')
    if acao == 'CORRIGIR_PREVISAO_BI':
        return (f"A fatura está correta. Corrigir o frete previsto na base interna (SAP, fonte do Power BI) de "
                f"{moeda(l['frete_previsto_bi'])} para {moeda(l['devido'])} e liberar o pagamento de {moeda(l['valor_cobrado'])}.")
    if acao == 'MONITORAR_CTE_COMPLEMENTAR':
        return f"Liberar o valor cobrado de {moeda(l['valor_cobrado'])} e monitorar eventual CT-e complementar da {t}."
    return f"Liberar o pagamento integral de {moeda(l['valor_cobrado'])}."


def motivo_revisao(l):
    """Por que (ou por que não) um humano precisa olhar este CT-e antes do pagamento."""
    if not l['requer_revisao_humana']:
        return 'não necessária (decisão dentro da alçada automática)'
    if l['contrato_generico']:
        return 'necessária (transportadora sem regra cadastrada)'
    if l['status'] == STATUS_ANALISE:
        return 'necessária (pendência a resolver antes da liberação)'
    if l['glosa'] >= ALCADA_REVISAO_HUMANA:
        return f'necessária (glosa acima da alçada de {moeda(ALCADA_REVISAO_HUMANA)})'
    if l['categoria_principal'] == 'TARIFA_ACIMA_TABELA':
        return 'necessária (excedente sem causa específica)'
    return 'necessária (mais de uma divergência na mesma nota)'


def cruzamento_das_fontes(l):
    """Seção 4 da defesa: o que cada uma das três fontes mostra sobre este CT-e."""
    fatura = [f"cobrado {moeda(l['valor_cobrado'])}"]
    if l['peso_taxado_kg']:
        fatura.append(f"peso taxado {kg(l['peso_taxado_kg'])}")
    if numero_valido(l['aliquota_icms']):
        fatura.append(f"ICMS {numero_br(l['aliquota_icms'], 0)}%")
    if l['frete_peso'] or l['ad_valorem'] or l['gris']:
        fatura.append(f"frete-peso {moeda(l['frete_peso'])}, ad valorem {moeda(l['ad_valorem'])}, GRIS {moeda(l['gris'])}")
    if l['servico_faturado']:
        fatura.append(f"serviço faturado {NOME_SERVICO[l['servico_faturado']]}")
    if l['tem_bi']:
        partes_bi = [f"NF {l['nf']}", rota_txt(l), f"peso real {kg(l['peso_real_kg'])}", f"volume {numero_br(l['volume_m3'], 3)} m³",
                     f"mercadoria {moeda(l['valor_mercadoria'])}", f"frete previsto {moeda(l['frete_previsto_bi'])}"]
        if l['servico_contratado']:
            partes_bi.append(f"serviço contratado {NOME_SERVICO[l['servico_contratado']]}")
        bi = ', '.join(partes_bi)
    else:
        bi = f"NF {l['nf']} não localizada no relatório"
    contrato = '' if l['contrato_generico'] else f" (contrato {l['transportadora']})"
    devido = 'sem base para recalcular' if l['devido'] is None else f"devido {moeda(l['devido'])}"
    regra = f"{MODALIDADES[l['modalidade']]['nome']}{contrato}: {devido}"
    if l['servico_inferido']:
        regra += f"; tarifa que explica a fatura: {NOME_SERVICO[l['servico_inferido']]}"
    if l['faixa_correta']:
        regra += f"; faixa correta {l['faixa_correta']}"
    numero_fatura = f" (fatura {l['fatura']})" if l['fatura'] else ''
    linhas = [
        f"• Fatura da transportadora: CT-e {l['numero_cte'] or 's/nº'}{numero_fatura}, {', '.join(fatura)}",
        f'• Relatório Power BI: {bi}',
        f'• Regra comercial: {regra}',
    ]
    if l['modalidade'] == 'COTACAO_SPOT' and l['cotacao_valor']:
        data = f" de {l['cotacao_data']}" if l['cotacao_data'] else ''
        icms = '+ ICMS' if l['cotacao_icms_incluso'] is False else 'com ICMS incluso'
        linhas.append(f"• Cotação WhatsApp {l['cotacao_id']}{data}: {moeda(l['cotacao_valor'])} {icms}")
    return linhas


def linha_memoria(m):
    """'• Frete-peso (369,2 kg × R$ 0,55/kg...): R$ 203,06 · na fatura: R$ 284,77 (divergente)'."""
    regra = f" ({m['regra']})" if m['regra'] else ''
    fatura = f" · na fatura: {m['fatura']}" if m['fatura'] else ''
    alerta = ' (divergente)' if m['alerta'] else ''
    return f"• {m['item']}{regra}: {m['referencia'] or '—'}{fatura}{alerta}"


def defesa_tecnica(l, data_auditoria):
    """Monta a defesa técnica completa (5 seções), pronta para colar no e-mail ao Financeiro."""
    if l['status'] == STATUS_CRITICA:
        secao3 = 'Divergências comprovadas'
    elif l['status'] == STATUS_ANALISE:
        secao3 = 'Pendências'
    else:
        secao3 = 'Verificações'
    if l['achados']:
        itens3 = [f"• {ACHADOS[a['codigo']]['rotulo']}{': ' + moeda(a['valor']) if a['valor'] else ''}. {a['detalhe']}."
                  for a in l['achados']]
    else:
        itens3 = ['• Valor da fatura conforme a regra contratual, dentro da tolerância.',
                  '• Frete previsto no Power BI confere com a regra.']
    fatura = f" · fatura {l['fatura']}" if l['fatura'] else ''
    linhas = [
        titulo_defesa(l),
        f"NF {l['nf']} · CT-e {l['numero_cte'] or 's/nº'} · {l['transportadora']}{fatura}",
        f"Regra aplicada: {MODALIDADES[l['modalidade']]['nome']}",
        'Cruzamento: fatura da transportadora × relatório Power BI × regra comercial',
        'Para: Contas a Pagar (Financeiro) · Cópia: Gestão de Logística',
        f'Data da auditoria: {data_auditoria}',
        '',
        '1. Fato apurado', fato_apurado(l), '',
        '2. Memória de cálculo', *[linha_memoria(m) for m in l['memoria']], '',
        f'3. {secao3}', *itens3, '',
        '4. Cruzamento das fontes', *cruzamento_das_fontes(l), '',
        '5. Encaminhamento', encaminhamento(l), f'Revisão humana: {motivo_revisao(l)}.', '',
        f'Auditoria automatizada pelo Agente Auditor IA (FreteAudit) · {VERSAO_SCHEMA}',
    ]
    return '\n'.join(linhas)                              # o * "espalha" a lista dentro da outra


# =============================================================================
# 11. PARECER ESTRUTURADO (o mesmo JSON Schema do system_prompt_agente.md)
# =============================================================================
def passos_verificacao(l):
    """Resumo do raciocínio em até 6 passos curtos (a "cadeia de pensamento" visível)."""
    passos = [f"Regra: {MODALIDADES[l['modalidade']]['nome']}."]
    if l['tem_bi']:
        passos.append(f"Fatura {moeda(l['valor_cobrado'])} × Power BI {moeda(l['frete_previsto_bi'])}: diferença de "
                      f"{moeda_sinal(l['diferenca'])} (tolerância {moeda(l['tolerancia'])}).")
    else:
        passos.append(f"Fatura {moeda(l['valor_cobrado'])}; NF não localizada no Power BI.")
    passos.append('Sem base para recalcular o frete.' if l['devido'] is None else f"Recalculado pela regra: devido {moeda(l['devido'])}.")
    for a in l['achados'][:2]:                            # no máximo os dois primeiros achados
        passos.append(f"{ACHADOS[a['codigo']]['rotulo']}: {a['detalhe']}.")
    passos.append(f"Decisão: {l['status']}.")
    return passos[:6]


def inconsistencias(l):
    """Dados que faltaram ou foram assumidos: o motor avisa em vez de esconder."""
    if not l['tem_bi']:
        return ['NF não localizada no Power BI: peso, rota e mercadoria indisponíveis.']
    lista = []
    if not l['uf_origem'] or not l['uf_destino']:
        lista.append('UF de origem ou destino ausente no Power BI: regra de ICMS não aplicada.')
    elif l['uf_origem'] == l['uf_destino']:
        lista.append('Operação interna: alíquota interna da UF não validada por esta regra.')
    if not l['peso_taxado_kg'] and l['modalidade'] != 'COTACAO_SPOT':
        lista.append('Peso taxado ausente na fatura.')
    if not l['numero_cte']:
        lista.append('Número do CT-e ausente: checagem de duplicidade não aplicada.')
    if l['modalidade'] == 'CALCULADORA' and not (l['frete_peso'] or l['ad_valorem'] or l['gris']):
        lista.append('Fatura sem componentes: efeito da cubagem estimado pela tarifa.')
    if l['modalidade'] == 'SELETOR_SERVICO' and not l['servico_contratado']:
        lista.append('Serviço contratado ausente no Power BI: assumido Standard.')
    if l['modalidade'] == 'COTACAO_SPOT' and l['cotacao_valor'] and l['cotacao_icms_incluso'] is None:
        lista.append('Condição de ICMS da cotação não informada: assumido incluso.')
    return lista


def parecer_local(l, data_auditoria):
    """Parecer completo gerado pelo próprio motor (modo offline e plano B se o LLM falhar)."""
    lista = inconsistencias(l)
    base = 0.97 if l['categoria_principal'] == 'APROVADO' else ACHADOS[l['categoria_principal']]['confianca']
    return {
        'nf': l['nf'],
        'numero_cte': l['numero_cte'] or None,
        'fatura': l['fatura'] or None,
        'transportadora': l['transportadora'],
        'modalidade': l['modalidade'],
        'status_auditoria': l['status'],
        'diagnostico': l['diagnostico'],
        'categoria_principal': l['categoria_principal'],
        'nf_localizada_power_bi': bool(l['tem_bi']),
        'valor_cobrado_fatura': l['valor_cobrado'],
        'valor_previsto_power_bi': l['frete_previsto_bi'],
        'valor_devido_regra': l['devido'],
        'valor_glosa_recomendado': l['glosa'],
        'valor_a_pagar': l['valor_a_pagar'],
        'nivel_confianca': arredondar(limitar(base - 0.05 * len(lista), 0, 1)),   # cada dado faltante tira 5 pontos
        'passos_verificacao': passos_verificacao(l),
        'memoria_calculo': [{'item': m['item'], 'regra': m['regra'], 'referencia': m['referencia'], 'fatura': m['fatura']}
                            for m in l['memoria']],
        'parecer': f'{fato_apurado(l)} {encaminhamento(l)}',
        'defesa_tecnica': defesa_tecnica(l, data_auditoria),
        'acao_recomendada': l['acao_recomendada'],
        'requer_revisao_humana': l['requer_revisao_humana'],
        'inconsistencias_detectadas': lista,
    }


# =============================================================================
# 12. INTEGRAÇÃO COM O LLM (Claude ou Gemini)
# =============================================================================
# Campos que pertencem ao motor: o LLM pode repeti-los, mas não pode mudá-los.
CAMPOS_DO_MOTOR = ['nf', 'numero_cte', 'fatura', 'transportadora', 'modalidade', 'status_auditoria', 'diagnostico',
                   'categoria_principal', 'nf_localizada_power_bi', 'valor_cobrado_fatura', 'valor_previsto_power_bi',
                   'valor_devido_regra', 'valor_glosa_recomendado', 'valor_a_pagar', 'acao_recomendada',
                   'requer_revisao_humana']
# Seções que toda defesa técnica precisa ter (o "3." varia: Divergências, Pendências ou Verificações)
SECOES_DEFESA = ['1. Fato apurado', '2. Memória de cálculo', '3. ', '4. Cruzamento das fontes', '5. Encaminhamento']


def carregar_system_prompt(caminho):
    """Lê o system_prompt_agente.md e devolve só o trecho entre os marcadores de início e fim."""
    arquivo = Path(caminho)
    if not arquivo.exists():
        raise ErroLLM(f'System prompt não encontrado: {caminho}')
    conteudo = arquivo.read_text(encoding='utf-8')
    if MARCADOR_INICIO in conteudo and MARCADOR_FIM in conteudo:
        conteudo = conteudo.split(MARCADOR_INICIO, 1)[1].split(MARCADOR_FIM, 1)[0]
    return conteudo.strip()


def chave_de_api(provedor):
    """Lê a chave da variável de ambiente. Chave nunca fica escrita no código nem no Git."""
    nomes = {'claude': ['ANTHROPIC_API_KEY'], 'gemini': ['GEMINI_API_KEY', 'GOOGLE_API_KEY']}[provedor]
    for nome in nomes:
        if os.environ.get(nome):
            return os.environ[nome]
    raise ErroLLM(f'Defina a variável de ambiente {nomes[0]} para usar o provedor {provedor}.')


def parametros_do_contrato(l):
    """Os parâmetros do contrato que o LLM precisa para entender a conta (sem o resto da tabela)."""
    contrato = resolver_contrato(l)
    if contrato['modalidade'] == 'TABELA_ICMS':
        rota = f"{l['uf_origem']}-{l['uf_destino']}"
        return {'fator_cubagem': contrato['fator_cubagem'], 'faixas_kg': contrato['faixas_kg'],
                'tabela_da_rota_sem_icms': contrato['tabela'].get(rota)}
    if contrato['modalidade'] == 'CALCULADORA':
        return {k: contrato[k] for k in ('fator_cubagem', 'tarifa_kg', 'frete_peso_minimo', 'ad_valorem_pct', 'gris_pct', 'gris_minimo')}
    if contrato['modalidade'] == 'SELETOR_SERVICO':
        return {'fator_cubagem': contrato['fator_cubagem'], 'servicos': contrato['servicos']}
    return {'fator_cubagem': contrato['fator_cubagem']}


def montar_payload(l, local, data_auditoria):
    """Monta o pacote de UMA divergência para o LLM: as 3 fontes + o que o motor já calculou."""
    payload = {
        'tarefa': f'Emita o parecer de auditoria deste CT-e no formato {VERSAO_SCHEMA}.',
        'data_auditoria': data_auditoria,
        'cte': {'nf': l['nf'], 'numero_cte': l['numero_cte'] or None, 'fatura': l['fatura'] or None,
                'transportadora': l['transportadora'], 'data_emissao': l['data_emissao'] or None},
        'fonte_1_fatura': {'valor_cobrado': l['valor_cobrado'], 'peso_taxado_kg': l['peso_taxado_kg'],
                           'frete_peso': l['frete_peso'], 'ad_valorem': l['ad_valorem'], 'gris': l['gris'],
                           'aliquota_icms': l['aliquota_icms'], 'servico_faturado': l['servico_faturado'] or None},
        'fonte_2_power_bi': ({'nf_localizada': True, 'uf_origem': l['uf_origem'], 'uf_destino': l['uf_destino'],
                              'peso_real_kg': l['peso_real_kg'], 'volume_m3': l['volume_m3'],
                              'valor_mercadoria': l['valor_mercadoria'], 'frete_previsto': l['frete_previsto_bi'],
                              'servico_contratado': l['servico_contratado'] or None}
                             if l['tem_bi'] else {'nf_localizada': False}),
        'fonte_3_regra_comercial': {'modalidade': l['modalidade'], 'nome_regra': MODALIDADES[l['modalidade']]['nome'],
                                    'modal': l['modal'], 'contrato_cadastrado': not l['contrato_generico'],
                                    'parametros': parametros_do_contrato(l)},
        'resultado_do_motor': {
            'status_auditoria': l['status'], 'categoria_principal': l['categoria_principal'], 'diagnostico': l['diagnostico'],
            'valor_devido_regra': l['devido'], 'valor_glosa_recomendado': l['glosa'], 'valor_a_pagar': l['valor_a_pagar'],
            'acao_recomendada': l['acao_recomendada'], 'requer_revisao_humana': l['requer_revisao_humana'],
            'motivo_revisao_humana': motivo_revisao(l), 'nivel_confianca': local['nivel_confianca'],
            'tolerancia': l['tolerancia'], 'aliquota_icms_esperada': l['aliquota_icms_esperada'],
            'diferenca_fatura_x_bi': l['diferenca'],
            'achados': [{'codigo': a['codigo'], 'descricao': ACHADOS[a['codigo']]['rotulo'], 'valor': a['valor'],
                         'detalhe': a['detalhe']} for a in l['achados']],
            'memoria_calculo': [{'item': m['item'], 'regra': m['regra'], 'referencia': m['referencia'],
                                 'fatura': m['fatura'], 'divergente': m['alerta']} for m in l['memoria']],
            'inconsistencias_detectadas': local['inconsistencias_detectadas'],
        },
    }
    if l['modalidade'] == 'COTACAO_SPOT':
        payload['fonte_3_regra_comercial']['cotacao_whatsapp'] = (
            {'id': l['cotacao_id'] or None, 'valor_acordado': l['cotacao_valor'], 'icms_incluso': l['cotacao_icms_incluso'],
             'data': l['cotacao_data'] or None} if l['cotacao_valor'] else None)
    return payload


def post_json(url, cabecalhos, corpo, tentativas=3, espera_max=60):
    """Faz um POST com JSON e devolve a resposta em JSON, tentando de novo em falhas temporárias.

    429 (limite de uso), 5xx (instabilidade) e 529 (sobrecarga) são temporários: espera e tenta de
    novo, cada vez esperando o dobro (2 s, 4 s...). É o chamado "backoff exponencial".
    """
    dados = json.dumps(corpo).encode('utf-8')
    for tentativa in range(1, tentativas + 1):
        pedido = urllib.request.Request(url, data=dados, headers=cabecalhos, method='POST')
        try:
            with urllib.request.urlopen(pedido, timeout=espera_max) as resposta:
                return json.loads(resposta.read().decode('utf-8'))
        except urllib.error.HTTPError as erro:
            detalhe = erro.read().decode('utf-8', errors='replace')[:400]   # mensagem de erro da API
            if erro.code in (408, 429, 500, 502, 503, 504, 529) and tentativa < tentativas:
                time.sleep(2 ** tentativa)
                continue
            raise ErroLLM(f'HTTP {erro.code}: {detalhe}') from erro
        except (urllib.error.URLError, TimeoutError) as erro:              # sem internet ou demorou demais
            if tentativa < tentativas:
                time.sleep(2 ** tentativa)
                continue
            raise ErroLLM(f'falha de conexão ({erro})') from erro
    raise ErroLLM('sem resposta da API')


def chamar_claude(system_prompt, mensagem, modelo):
    """Chama a Messages API da Anthropic (https://docs.claude.com/en/api/messages)."""
    cabecalhos = {'x-api-key': chave_de_api('claude'), 'anthropic-version': '2023-06-01', 'content-type': 'application/json'}
    corpo = {
        'model': modelo,
        'max_tokens': 4000,
        'temperature': 0,             # 0 = respostas mais estáveis (auditoria não é lugar de criatividade)
        # cache_control guarda o system prompt (longo e igual em todas as chamadas) em cache: mais barato e rápido
        'system': [{'type': 'text', 'text': system_prompt, 'cache_control': {'type': 'ephemeral'}}],
        'messages': [{'role': 'user', 'content': mensagem}],
    }
    try:
        resposta = post_json(URL_CLAUDE, cabecalhos, corpo)
    except ErroLLM as erro:
        if 'temperature' not in str(erro):
            raise
        corpo.pop('temperature')      # alguns modelos recentes não aceitam o parâmetro: tenta sem ele
        resposta = post_json(URL_CLAUDE, cabecalhos, corpo)
    if resposta.get('stop_reason') == 'max_tokens':
        raise ErroLLM('a resposta foi cortada pelo limite de tokens')
    return ''.join(bloco.get('text', '') for bloco in resposta.get('content', []) if bloco.get('type') == 'text')


def chamar_gemini(system_prompt, mensagem, modelo):
    """Chama o generateContent do Gemini (https://ai.google.dev/api/generate-content)."""
    cabecalhos = {'x-goog-api-key': chave_de_api('gemini'), 'content-type': 'application/json'}
    corpo = {
        'systemInstruction': {'parts': [{'text': system_prompt}]},
        'contents': [{'role': 'user', 'parts': [{'text': mensagem}]}],
        # responseMimeType força a saída em JSON; o limite é folgado porque modelos que "pensam"
        # gastam parte dos tokens raciocinando antes de responder
        'generationConfig': {'temperature': 0, 'responseMimeType': 'application/json', 'maxOutputTokens': 8192},
    }
    resposta = post_json(URL_GEMINI.format(modelo=modelo), cabecalhos, corpo)
    candidatos = resposta.get('candidates') or []
    if not candidatos:
        raise ErroLLM(f"resposta sem conteúdo ({resposta.get('promptFeedback', {})})")
    partes = candidatos[0].get('content', {}).get('parts', [])
    return ''.join(p.get('text', '') for p in partes if not p.get('thought'))   # ignora partes de "pensamento"


def chamar_llm(provedor, modelo, system_prompt, mensagem):
    """Escolhe o provedor. Trocar de IA é mudar um parâmetro, não reescrever o sistema."""
    funcao = {'claude': chamar_claude, 'gemini': chamar_gemini}[provedor]
    texto = funcao(system_prompt, mensagem, modelo)
    if not texto.strip():
        raise ErroLLM('resposta vazia')
    return texto


def extrair_json(texto):
    """Tira o JSON da resposta, mesmo que o modelo tenha embrulhado em ```json ... ```."""
    limpo = re.sub(r'^```(?:json)?\s*|\s*```$', '', texto.strip())
    inicio, fim = limpo.find('{'), limpo.rfind('}')
    if inicio < 0 or fim <= inicio:
        raise ErroLLM('a resposta não contém um objeto JSON')
    try:
        return json.loads(limpo[inicio:fim + 1])
    except json.JSONDecodeError as erro:
        raise ErroLLM(f'JSON inválido na resposta ({erro.msg})') from erro


def iguais(a, b):
    """Compara dois valores; números com folga de meio centavo."""
    if numero_valido(a) and numero_valido(b):
        return abs(a - b) < 0.005
    return a == b


def validar_resposta(resposta, local, origem):
    """Confere a resposta do LLM contra o motor e monta o parecer final.

    Regras de segurança ("guardrails"):
      - números, status, categoria e ação SEMPRE vêm do motor; se o LLM divergir, fica registrado;
      - o texto (parecer, defesa e passos) vem do LLM só se estiver completo;
      - a confiança final é a MENOR entre a do motor e a do LLM (postura conservadora).
    """
    if not isinstance(resposta, dict):
        raise ErroLLM('a resposta não é um objeto JSON')
    final = dict(local)                                    # começa com tudo do motor
    notas = []
    for campo in CAMPOS_DO_MOTOR:
        if campo in resposta and not iguais(resposta[campo], local[campo]):
            notas.append(f'O LLM informou {campo} = {resposta[campo]!r}; mantido o valor do motor ({local[campo]!r}).')
    parecer = resposta.get('parecer')
    if isinstance(parecer, str) and parecer.strip():
        final['parecer'] = parecer.strip()
    else:
        notas.append('O LLM não devolveu o parecer; usado o texto do motor.')
    defesa = resposta.get('defesa_tecnica')
    if isinstance(defesa, str) and all(secao in defesa for secao in SECOES_DEFESA):
        glosa = local['valor_glosa_recomendado']
        if glosa > 0 and moeda(glosa) not in defesa.replace('\u00a0', ' '):
            notas.append('A defesa do LLM não cita o valor da glosa; usada a defesa do motor.')
        else:
            final['defesa_tecnica'] = defesa.strip()
    else:
        notas.append('A defesa do LLM não tem as 5 seções obrigatórias; usada a defesa do motor.')
    passos = resposta.get('passos_verificacao')
    if isinstance(passos, list) and 3 <= len(passos) <= 6 and all(isinstance(p, str) and p.strip() for p in passos):
        final['passos_verificacao'] = [p.strip() for p in passos]
    confianca = resposta.get('nivel_confianca')
    if numero_valido(confianca) and 0 <= confianca <= 1:
        final['nivel_confianca'] = arredondar(min(confianca, local['nivel_confianca']))
    extras = resposta.get('inconsistencias_detectadas')
    extras = [i.strip() for i in extras if isinstance(i, str) and i.strip()] if isinstance(extras, list) else []
    final['inconsistencias_detectadas'] = local['inconsistencias_detectadas'] + \
        [i for i in extras if i not in local['inconsistencias_detectadas']] + notas
    final['gerado_por'] = origem
    return final


def gerar_pareceres(resultados, provedor, modelo, caminho_prompt, limite, data_auditoria):
    """Gera um parecer por CT-e. Só as divergências vão para o LLM (aprovados não gastam chamada)."""
    pareceres = []
    for l in resultados:                                   # primeiro, o parecer do motor para todos
        p = parecer_local(l, data_auditoria)
        p['gerado_por'] = 'motor_local'
        pareceres.append(p)
    uso = {'enviados': 0, 'sucesso': 0, 'falhas': 0, 'fora_do_limite': 0}
    payloads = []
    if provedor == 'offline':
        return pareceres, payloads, uso
    system_prompt = carregar_system_prompt(caminho_prompt)   # falha cedo se o arquivo não existir
    chave_de_api(provedor)                                   # falha cedo se a chave não existir
    divergentes = [i for i, l in enumerate(resultados) if l['status'] != STATUS_APROVADO]
    enviar = divergentes[:limite]
    uso['fora_do_limite'] = len(divergentes) - len(enviar)
    print(f'Enviando {plural(len(enviar), "divergência", "divergências")} para o {provedor} ({modelo})...')
    for n, i in enumerate(enviar, start=1):
        l, local = resultados[i], pareceres[i]
        payload = montar_payload(l, local, data_auditoria)
        payloads.append(payload)
        mensagem = json.dumps(payload, ensure_ascii=False, indent=1)
        inicio = time.time()
        uso['enviados'] += 1
        try:
            try:
                resposta = extrair_json(chamar_llm(provedor, modelo, system_prompt, mensagem))
            except ErroLLM as erro:
                if 'JSON' not in str(erro):
                    raise
                # Segunda chance: lembra o modelo de devolver só o JSON
                lembrete = mensagem + '\n\nResponda somente com o objeto JSON do schema, sem texto antes ou depois.'
                resposta = extrair_json(chamar_llm(provedor, modelo, system_prompt, lembrete))
            pareceres[i] = validar_resposta(resposta, local, f'{provedor}:{modelo}')
            uso['sucesso'] += 1
            alertas = len(pareceres[i]['inconsistencias_detectadas']) - len(local['inconsistencias_detectadas'])
            print(f'  [{n}/{len(enviar)}] NF {l["nf"]} · {l["transportadora"]}: ok em {time.time() - inicio:.1f} s'
                  + (f' ({plural(alertas, "ajuste do guardrail", "ajustes do guardrail")})' if alertas else ''))
        except ErroLLM as erro:
            uso['falhas'] += 1
            local['inconsistencias_detectadas'] = local['inconsistencias_detectadas'] + \
                [f'Parecer redigido pelo motor local: o {provedor} falhou ({erro}).']
            print(f'  [{n}/{len(enviar)}] NF {l["nf"]}: falhou ({erro}); mantido o parecer do motor')
    if uso['fora_do_limite']:
        print(f'Aviso: {plural(uso["fora_do_limite"], "divergência ficou", "divergências ficaram")} com o parecer do '
              f'motor por causa do limite de {limite} chamadas (--limite-llm).')
    return pareceres, payloads, uso


# =============================================================================
# 13. SAÍDAS: PLANILHA PADRONIZADA, PARECERES E DEFESAS
# =============================================================================
def cel_texto(valor):
    """Protege contra "CSV injection": texto que começa com = + - @ vira texto literal no Excel."""
    s = '' if valor is None else str(valor)
    return "'" + s if re.match(r'^[=+\-@\t\r]', s) else s


def cel_num(valor, casas=2):
    """Número com vírgula decimal e sem milhar (o Excel em português lê como número)."""
    return str(_quantizar(valor, casas, exato=True)).replace('.', ',') if numero_valido(valor) else ''


def servico(codigo):
    return NOME_SERVICO.get(codigo, '') if codigo else ''


# Mesmas 45 colunas, na mesma ordem, da exportação do painel: o CSV gerado aqui pode ser
# reaberto no index.html (ele reconhece a planilha como "combinada").
COLUNAS_PLANILHA = [
    ('Fatura', lambda l: cel_texto(l['fatura'])),
    ('CT-e', lambda l: cel_texto(l['numero_cte'])),
    ('NF', lambda l: cel_texto(l['nf'])),
    ('Transportadora', lambda l: cel_texto(l['transportadora'])),
    ('Regra / Modalidade', lambda l: MODALIDADES[l['modalidade']]['nome']),
    ('Modal', lambda l: 'Aéreo' if l['modal'] == 'AEREO' else 'Rodoviário'),
    ('NF no Power BI', lambda l: 'SIM' if l['tem_bi'] else 'NÃO'),
    ('UF Origem', lambda l: l['uf_origem']),
    ('UF Destino', lambda l: l['uf_destino']),
    ('Data Emissão CT-e', lambda l: l['data_emissao']),
    ('Valor Cobrado Fatura', lambda l: cel_num(l['valor_cobrado'])),
    ('Peso Taxado Fatura (kg)', lambda l: cel_num(l['peso_taxado_kg'])),
    ('Frete-Peso Fatura', lambda l: cel_num(l['frete_peso']) if l['frete_peso'] else ''),
    ('Ad Valorem Fatura', lambda l: cel_num(l['ad_valorem']) if l['ad_valorem'] else ''),
    ('GRIS Fatura', lambda l: cel_num(l['gris']) if l['gris'] else ''),
    ('Alíquota ICMS Fatura (%)', lambda l: cel_num(l['aliquota_icms'], 0)),
    ('Serviço Faturado', lambda l: servico(l['servico_faturado'])),
    ('Peso Real BI (kg)', lambda l: cel_num(l['peso_real_kg']) if l['tem_bi'] else ''),
    ('Volume BI (m3)', lambda l: cel_num(l['volume_m3'], 3) if l['tem_bi'] else ''),
    ('Valor Mercadoria BI', lambda l: cel_num(l['valor_mercadoria']) if l['tem_bi'] else ''),
    ('Serviço Contratado', lambda l: servico(l['servico_contratado'])),
    ('Frete Previsto Power BI', lambda l: cel_num(l['frete_previsto_bi'])),
    ('Cotação ID', lambda l: cel_texto(l['cotacao_id'])),
    ('Cotação Valor', lambda l: cel_num(l['cotacao_valor'])),
    ('Cotação ICMS Incluso', lambda l: {True: 'SIM', False: 'NÃO'}.get(l['cotacao_icms_incluso'], '')),
    ('Cotação Data', lambda l: cel_texto(l['cotacao_data'])),
    ('Peso Cubado (kg)', lambda l: cel_num(l['peso_cubado_kg'])),
    ('Peso Taxável (kg)', lambda l: cel_num(l['peso_taxavel_kg'])),
    ('Alíquota ICMS Esperada (%)', lambda l: cel_num(l['aliquota_icms_esperada'], 0)),
    ('Serviço Inferido', lambda l: servico(l['servico_inferido'])),
    ('Devido pela Regra', lambda l: cel_num(l['devido'])),
    ('Diferença Fatura x BI (R$)', lambda l: cel_num(l['diferenca'])),
    ('Diferença Fatura x BI (%)', lambda l: cel_num(l['diferenca_pct'] * 100) if numero_valido(l['diferenca_pct']) else ''),
    ('Excedente sobre a Regra (R$)', lambda l: cel_num(l['excedente'])),
    ('Tolerância (R$)', lambda l: cel_num(l['tolerancia'])),
    ('Status Auditoria', lambda l: l['status']),
    ('Diagnóstico IA', lambda l: cel_texto(l['diagnostico'])),
    ('Categoria Principal', lambda l: l['categoria_principal']),
    ('Ação Recomendada', lambda l: ACAO_LABEL.get(l['acao_recomendada'], '')),
    ('Valor a Glosar (R$)', lambda l: cel_num(l['glosa'])),
    ('Valor a Pagar (R$)', lambda l: cel_num(l['valor_a_pagar'])),
    ('Requer Revisão Humana', lambda l: 'SIM' if l['requer_revisao_humana'] else 'NÃO'),
    ('Achados', lambda l: ' | '.join(a['codigo'] for a in l['achados'])),
    ('Defesa Técnica (resumo)', lambda l: cel_texto(fato_apurado(l))),
    ('Auditado Em', lambda l: l['auditado_em']),
]


def salvar_planilha(resultados, caminho):
    """Planilha padronizada: ";" como separador e vírgula decimal (abre direto no Excel em pt-BR)."""
    linhas = [[funcao(l) for _, funcao in COLUNAS_PLANILHA] for l in resultados]
    tabela = pd.DataFrame(linhas, columns=[nome for nome, _ in COLUNAS_PLANILHA])
    # utf-8-sig grava o BOM, que faz o Excel reconhecer os acentos; \r\n é a quebra de linha do Windows
    tabela.to_csv(caminho, sep=';', index=False, encoding='utf-8-sig', lineterminator='\r\n')


def salvar_pareceres(pareceres, caminho):
    """Um parecer JSON por linha (formato JSONL: fácil de carregar em banco de dados ou no Pandas)."""
    with open(caminho, 'w', encoding='utf-8') as arquivo:
        for p in pareceres:
            arquivo.write(json.dumps(p, ensure_ascii=False) + '\n')


def salvar_defesas(resultados, pareceres, caminho):
    """Um .txt com as defesas: primeiro as glosas (maior valor antes), depois os bloqueios."""
    pares = [(l, p) for l, p in zip(resultados, pareceres) if l['status'] != STATUS_APROVADO]
    if not pares:                                          # nenhuma divergência: lista os aprovados
        pares = list(zip(resultados, pareceres))
    ordem = {STATUS_CRITICA: 0, STATUS_ANALISE: 1, STATUS_APROVADO: 2}
    pares.sort(key=lambda par: (ordem[par[0]['status']], -par[0]['glosa']))
    glosa = somar([l['glosa'] for l, _ in pares])
    cabecalho = ['FreteAudit · Defesas técnicas para o Financeiro',
                 f"Gerado em {datetime.now():%d/%m/%Y %H:%M} · {plural(len(pares), 'documento', 'documentos')} · glosa total {moeda(glosa)}",
                 '']
    separador = '\n\n' + '─' * 72 + '\n\n'
    conteudo = '\n'.join(cabecalho) + separador + separador.join(p['defesa_tecnica'] for _, p in pares)
    Path(caminho).write_text(conteudo + '\n', encoding='utf-8')


def salvar_payloads(payloads, caminho):
    """Opcional (--salvar-payloads): o que foi enviado ao LLM, para conferência e depuração."""
    with open(caminho, 'w', encoding='utf-8') as arquivo:
        for payload in payloads:
            arquivo.write(json.dumps(payload, ensure_ascii=False) + '\n')


def imprimir_resumo(resultados, estatisticas, uso, provedor):
    """Resumo executivo no terminal, com um groupby do Pandas por transportadora."""
    contagem = {s: sum(1 for l in resultados if l['status'] == s) for s in (STATUS_APROVADO, STATUS_CRITICA, STATUS_ANALISE)}
    glosa = somar([l['glosa'] for l in resultados])
    cobrado = estatisticas['cobrado']
    print('\n' + '═' * 72)
    print('AUDITORIA DE FRETES · fatura × Power BI × regra comercial')
    print('═' * 72)
    print(f"Fontes: {plural(estatisticas['ctes'], 'CT-e', 'CT-es')} da fatura "
          f"({plural(estatisticas['transportadoras'], 'transportadora', 'transportadoras')}, {moeda(cobrado)}) · "
          f"{plural(estatisticas['notas_bi'], 'nota', 'notas')} do Power BI · "
          f"{plural(estatisticas['cotacoes'], 'cotação', 'cotações')} WhatsApp")
    print(f"Cruzamento pela NF: {estatisticas['cruzados']} cruzados · {estatisticas['sem_bi']} sem NF no BI · "
          f"{plural(estatisticas['sem_cobranca'], 'nota do BI sem cobrança', 'notas do BI sem cobrança')}")
    print(f"Resultado: {contagem[STATUS_APROVADO]} aprovados · {contagem[STATUS_CRITICA]} com divergência crítica · "
          f"{contagem[STATUS_ANALISE]} em análise")
    print(f"Glosa recomendada: {moeda(glosa)} ({pct(glosa / cobrado if cobrado else 0)} do faturado) · "
          f"{plural(sum(1 for l in resultados if l['requer_revisao_humana']), 'CT-e pede', 'CT-es pedem')} revisão humana")
    if provedor != 'offline':
        print(f"LLM ({provedor}): {uso['sucesso']} pareceres redigidos · {uso['falhas']} falhas · "
              f"{uso['fora_do_limite']} acima do limite")

    # groupby: agrupa as linhas por transportadora e soma as colunas numéricas de cada grupo
    df = pd.DataFrame([{'Transportadora': l['transportadora'], 'Regra': MODALIDADES[l['modalidade']]['curto'],
                        'CT-es': 1, 'Divergências': int(l['status'] != STATUS_APROVADO),
                        'Cobrado': l['valor_cobrado'], 'Glosa': l['glosa']} for l in resultados])
    tabela = df.groupby(['Transportadora', 'Regra'], sort=False).sum().sort_values('Glosa', ascending=False)
    tabela['Cobrado'] = tabela['Cobrado'].map(moeda)        # formata só para exibir
    tabela['Glosa'] = tabela['Glosa'].map(moeda)
    print('\nPor transportadora:')
    print(tabela.to_string())

    maiores = sorted((l for l in resultados if l['glosa'] > 0), key=lambda l: -l['glosa'])[:5]
    if maiores:
        print('\nMaiores glosas:')
        for l in maiores:
            print(f"  NF {l['nf']} · {l['transportadora']} · {l['diagnostico']} · {moeda(l['glosa'])}")


# =============================================================================
# 14. LINHA DE COMANDO
# =============================================================================
def ler_argumentos(argv=None):
    pasta_do_script = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        prog='auditor_ia.py',
        description='Audita a fatura da transportadora contra o Power BI e a regra comercial de cada contrato.',
        epilog='Exemplo: python auditor_ia.py --fatura fatura.csv --power-bi bi.csv --cotacoes cotacoes.csv --provedor claude',
    )
    parser.add_argument('--fatura', required=True, help='CSV da fatura (CT-es cobrados) ou a planilha padronizada do painel')
    parser.add_argument('--power-bi', dest='power_bi', help='CSV do relatório do Power BI (base interna do SAP)')
    parser.add_argument('--cotacoes', help='CSV das cotações spot registradas no WhatsApp (opcional)')
    parser.add_argument('--provedor', choices=['offline', 'claude', 'gemini'], default='offline',
                        help='quem redige os pareceres das divergências (padrão: offline, sem custo)')
    parser.add_argument('--modelo', help='modelo do provedor (padrão: claude-sonnet-5-5 ou gemini-flash-latest)')
    parser.add_argument('--prompt', default=str(pasta_do_script / 'system_prompt_agente.md'),
                        help='arquivo do system prompt do agente')
    parser.add_argument('--saida', default='saida', help='pasta dos arquivos gerados (padrão: ./saida)')
    parser.add_argument('--limite-llm', dest='limite_llm', type=int, default=LIMITE_CHAMADAS_LLM,
                        help=f'máximo de chamadas ao LLM por execução (padrão: {LIMITE_CHAMADAS_LLM})')
    parser.add_argument('--salvar-payloads', dest='salvar_payloads', action='store_true',
                        help='salva em payloads_llm.jsonl o que foi enviado ao LLM')
    return parser.parse_args(argv)


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')           # terminal sem suporte a "→" não derruba o script
    args = ler_argumentos(argv)
    modelo = args.modelo or os.environ.get('AUDITOR_MODELO') or MODELO_PADRAO.get(args.provedor, '')
    try:
        # Passos 1 e 2: ler as três fontes e cruzar pela NF
        fatura, bi, cotacoes, avisos = carregar_fontes(args.fatura, args.power_bi, args.cotacoes)
        for aviso in avisos:
            print(f'Aviso: {aviso}')
        registros, estatisticas = cruzar(fatura, bi, cotacoes)
        # Passos 3 e 4: recalcular pela regra de cada contrato e validar
        resultados = auditar_base(registros)
        # Passo 5: pareceres (só as divergências vão para o LLM, se houver provedor)
        data_auditoria = datetime.now().strftime('%d/%m/%Y')
        pareceres, payloads, uso = gerar_pareceres(resultados, args.provedor, modelo, args.prompt,
                                                   args.limite_llm, data_auditoria)
    except (ErroEntrada, ErroLLM) as erro:
        print(f'Erro: {erro}', file=sys.stderr)
        return 1
    # Passo 6: salvar as saídas
    pasta = Path(args.saida)
    pasta.mkdir(parents=True, exist_ok=True)
    arquivos = {'planilha': pasta / 'auditoria_fretes_padronizada.csv', 'pareceres': pasta / 'pareceres_auditoria.jsonl',
                'defesas': pasta / 'defesas_tecnicas.txt'}
    salvar_planilha(resultados, arquivos['planilha'])
    salvar_pareceres(pareceres, arquivos['pareceres'])
    salvar_defesas(resultados, pareceres, arquivos['defesas'])
    if args.salvar_payloads:
        arquivos['payloads'] = pasta / 'payloads_llm.jsonl'
        salvar_payloads(payloads, arquivos['payloads'])
    imprimir_resumo(resultados, estatisticas, uso, args.provedor)
    print('\nArquivos gerados:')
    for caminho in arquivos.values():
        print(f'  {caminho}')
    return 0


if __name__ == '__main__':          # só roda o main() quando o arquivo é executado, não quando é importado
    sys.exit(main())
