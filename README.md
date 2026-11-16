# Stays Content Automation (`stays-content-automation`)

Ferramenta de linha de comando (CLI) em Python para Linux que automatiza a extração, organização via API do Claude (Anthropic), revisão interativa e preenchimento seguro dos campos personalizados de imóveis na plataforma **Stays.net** utilizando **Playwright**.

---

## Sumário

1. [Arquitetura e Segurança](#1-arquitetura-e-segurança)
2. [Divergência Documentada: 36 vs 37 Campos](#2-divergência-documentada-36-vs-37-campos)
3. [Requisitos do Sistema (Linux)](#3-requisitos-do-sistema-linux)
4. [Instalação Passo a Passo (com `uv` ou `venv`)](#4-instalação-passo-a-passo-com-uv-ou-venv)
5. [Configuração do `.env` e Credenciais](#5-configuração-do-env-e-credenciais)
6. [Modo de Inspeção de Seletores (`inspect`)](#6-modo-de-inspeção-de-seletores-inspect)
7. [Execução em Modo `dry-run` (URL, Arquivo e Interativo)](#7-execução-em-modo-dry-run-url-arquivo-e-interativo)
8. [Revisão dos Artefatos Gerados](#8-revisão-dos-artefatos-gerados)
9. [Execução com Salvamento (`--save`)](#9-execução-com-salvamento---save)
10. [Retomada Após Falha (`--resume`)](#10-retomada-após-falha---resume)
11. [Ajuste de Seletores e Campos Personalizados](#11-ajuste-de-seletores-e-campos-personalizados)
12. [Execução dos Testes Automatizados](#12-execução-dos-testes-automatizados)
13. [Execução Opcional com Docker](#13-execução-opcional-com-docker)
14. [Solução de Problemas Comuns](#14-solução-de-problemas-comuns)

---

## 1. Arquitetura e Segurança

- **Zero segredos em código**: credenciais do Stays e chave da API da Anthropic são lidas exclusivamente de variáveis de ambiente (`.env`).
- **Filtro e Sanitização Universal (`security.py`)**: senhas, tokens, cookies e chaves `sk-ant-...` são interceptados antes de qualquer gravação em logs (`execution.log`) ou artefatos JSON/CSV/HTML. Senhas de Wi-Fi (`********`) e códigos de fechadura/cofre (`****`) são mascarados automaticamente nos artefatos e tabelas de prévia.
- **Modo `dry-run` por padrão**: a ferramenta preenche os campos na interface, captura evidências (`before_fill.png` e `after_fill.png`), mas **jamais clica em Salvar** sem a flag `--save` e sem dupla confirmação humana.
- **Respeito a CAPTCHA e MFA**: caso o Stays apresente CAPTCHA ou autenticação em duas etapas, a automação não tenta contornar; pausa e solicita que o operador conclua a verificação na janela visível do navegador.

---

## 2. Divergência Documentada: 36 vs 37 Campos

Embora a descrição inicial mencione **36 campos personalizados**, a relação oficial especificada possui **37 itens distintos** (incluindo o campo nº 31 cujo rótulo no Stays é `"."`).

- Nenhum campo foi excluído silenciosamente.
- Todos os **37 campos** estão versionados em `config/custom_fields.yaml`.
- O campo `"."` (`field_key: "dot_placeholder"`) possui `allow_auto_fill: false` por padrão para nunca ser preenchido sem regra explícita.
- A divergência (`expected_count: 36` vs `actual_count: 37`) é detectada e registrada nos avisos do terminal e em `execution_report.json`.

---

## 3. Requisitos do Sistema (Linux)

- Sistema operacional Linux (Ubuntu 22.04+, Debian 12+, Fedora ou equivalente)
- Python **3.11** ou superior
- Acesso de rede à plataforma Stays e à API da Anthropic
- Ambiente gráfico X11/Wayland (para o modo visível `--no-headless` e `inspect`) ou `xvfb` em servidores sem monitor

---

## 4. Instalação Passo a Passo (com `uv` ou `venv`)

### 4.1. Clonar ou acessar a pasta do projeto

```bash
cd /home/luramalho/Documents/stays-content-automation
```

### 4.2. Opção Principal: Usando `uv` (Recomendado)

Caso ainda não possua o `uv` instalado no Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env" 2>/dev/null || export PATH="$HOME/.local/bin:$PATH"
```

Sincronize as dependências e instale o navegador Chromium do Playwright:

```bash
cp .env.example .env
uv sync --extra dev
uv run playwright install chromium
```

> Caso seja a primeira vez instalando o Playwright na máquina Linux e faltem bibliotecas gráficas do sistema operacional, execute:
> ```bash
> uv run playwright install-deps chromium
> ```

### 4.3. Opção Alternativa: Usando `python3 -m venv` e `pip`

Caso prefira não utilizar o `uv`, você pode instalar tudo com a biblioteca padrão do Python:

```bash
cp .env.example .env
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
playwright install chromium
```

---

## 5. Configuração do `.env` e Credenciais

Edite o arquivo `.env` na raiz do projeto (ele já está listado no `.gitignore` e nunca será versionado):

```bash
nano .env
```

Preencha as variáveis de acordo com o provedor de LLM desejado:

#### Opção A: Execução Local com Ollama (Padrão - Sem Chave de API)

```dotenv
STAYS_BASE_URL=https://ssmt.stays.com.br/i/home
STAYS_LOGIN=seu_usuario_ou_email_stays
STAYS_PASSWORD=sua_senha_stays

# Provedor local gratuito sem necessidade de chaves de API:
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3:latest
OLLAMA_TIMEOUT_SECONDS=600

HEADLESS=false
DRY_RUN=true
```

> **Dica para CPU**: Caso execute em computadores sem placa de vídeo dedicada (GPU), modelos menores como `llama3.2:3b` ou `qwen2.5:3b` executam a extração com rapidez (`ollama pull llama3.2:3b`).

#### Opção B: Execução em Nuvem com API Anthropic Claude

```dotenv
STAYS_BASE_URL=https://ssmt.stays.com.br/i/home
STAYS_LOGIN=seu_usuario_ou_email_stays
STAYS_PASSWORD=sua_senha_stays

LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-api03-sua-chave-aqui
CLAUDE_MODEL=claude-3-5-sonnet-latest

HEADLESS=false
DRY_RUN=true
```

#### Opção C: Claude via Claude Code CLI (Sem Chave de API)

Usa o executável `claude` em modo headless (`claude -p`), autenticado com o login da sua assinatura do Claude Code (Pro/Max). Não exige `ANTHROPIC_API_KEY`; o consumo é descontado dos limites da assinatura. Indicado para uso pessoal na própria máquina.

```dotenv
LLM_PROVIDER=claude_cli
CLAUDE_CLI_PATH=claude
CLAUDE_CLI_MODEL=sonnet
CLAUDE_CLI_TIMEOUT_SECONDS=300
```

Pré-requisito: Claude Code instalado e logado (rode `claude` uma vez e faça login). A chamada é feita sem ferramentas (`--tools ""`), em diretório temporário isolado e sem `ANTHROPIC_API_KEY` no ambiente do processo filho, garantindo o uso da assinatura.

---

## 6. Modo de Inspeção de Seletores (`inspect`)

Antes de rodar o preenchimento pela primeira vez em uma instância do Stays, execute o modo `inspect`. Ele abre o navegador visível, autentica, navega até o imóvel, descobre os rótulos e textareas/inputs associados, gera um HTML sanitizado e grava `field_mapping.json`:

```bash
uv run python -m stays_automation inspect \
  --target-url "https://ssmt.stays.com.br/i/apartment/AB25I/setup"
```

---

## 7. Execução em Modo `dry-run` (URL, Arquivo e Interativo)

No modo `--dry-run` (ativo por padrão), a ferramenta extrai os dados, estrutura os 37 campos via Claude, exibe a tabela de prévia no terminal, resolve eventuais conflitos com valores existentes e preenche os campos no navegador para inspeção visual, **sem clicar em Salvar**.

### 7.1. Usando uma URL pública como fonte (`--source-url`)

```bash
uv run python -m stays_automation \
  --target-url "https://ssmt.stays.com.br/i/apartment/AB25I/setup" \
  --source-url "https://www.multitemporada.com/pt/apartment/SR08H" \
  --dry-run
```

### 7.2. Usando um arquivo de texto bruto (`--source-file`)

```bash
uv run python -m stays_automation \
  --target-url "https://ssmt.stays.com.br/i/apartment/AB25I/setup" \
  --source-file "./examples/entrada_exemplo.txt" \
  --dry-run
```

### 7.3. Usando entrada interativa pelo terminal (`--paste`)

Com `--paste`, a ferramenta solicitará que você cole o texto diretamente no terminal (digite `FIM` em uma nova linha para concluir):

```bash
uv run python -m stays_automation \
  --target-url "https://ssmt.stays.com.br/i/apartment/AB25I/setup" \
  --paste \
  --dry-run
```

### 7.3.1. Somente o código do imóvel (`--id`)

Informando apenas o código, a URL do Stays é montada automaticamente
(`https://ssmt.stays.com.br/i/apartment/<ID>/setup`) e, se nenhuma fonte for informada
(`--source-url`, `--source-file` ou `--paste`), as informações são buscadas no site da
Multitemporada (`https://www.multitemporada.com/pt/apartment/<ID>`):

```bash
uv run python -m stays_automation --id AB02I --dry-run
uv run python -m stays_automation --id AB02I --save
```

Se a página da Multitemporada não existir (HTTP 404), a execução é interrompida antes de chamar a LLM.
Se `--id` e `--target-url` forem informados com códigos diferentes, a execução é rejeitada.

### 7.3.2. Vários sites como fonte

`--source-url` pode ser repetido (ou receber URLs separadas por vírgula) e pode ser combinado com
`--source-file`. O conteúdo de todas as fontes é unido, identificado por fonte, antes de ir para a LLM:

```bash
uv run python -m stays_automation --id AB02I \
  --source-url "https://www.multitemporada.com/pt/apartment/AB02I" \
  --source-url "https://site-da-pousada.com.br" \
  --source-file "./notas_extras.txt" \
  --dry-run
```

Os modelos de URL podem ser alterados no `.env` com `STAYS_PROPERTY_URL_TEMPLATE` e
`DEFAULT_SOURCE_URL_TEMPLATE` (use o marcador `{property_id}`).

### 7.4. Opções adicionais de linha de comando

```bash
uv run python -m stays_automation \
  --target-url "https://ssmt.stays.com.br/i/apartment/AB25I/setup" \
  --source-file "./examples/entrada_exemplo.txt" \
  --id "AB25I" \
  --llm-provider ollama \
  --ollama-model "llama3:latest" \
  --confidence-threshold 0.85 \
  --output-dir "./artifacts" \
  --no-headless \
  --dry-run
```

Opções de Provedor LLM suportadas:
- `--llm-provider {ollama,anthropic,claude_cli}`: seleciona se a extração usará Ollama (local), Anthropic (API) ou o Claude Code CLI (assinatura, sem chave).
- `--ollama-url URL`: URL do servidor Ollama (padrão: `http://localhost:11434`).
- `--ollama-model NOME`: modelo local no Ollama (ex.: `llama3:latest`, `llama3.2:3b`, `qwen2.5:3b`).
- `--ollama-timeout SEGUNDOS`: tempo limite em segundos para geração de respostas locais (padrão: `600`).
- `--model NOME`: atalho genérico para definir o modelo ativo (Ollama, Claude API ou Claude Code CLI, ex.: `sonnet`, `opus`).

> **Nota de segurança**: Informar `--save` e `--dry-run` simultaneamente é rejeitado imediatamente com erro (`exit code 2`).

---

## 8. Revisão dos Artefatos Gerados

Cada execução cria um diretório exclusivo em `artifacts/YYYYMMDD_HHMMSS_PROPERTY_ID/` contendo:

| Arquivo | Descrição |
| :--- | :--- |
| `input_original.txt` | Conteúdo original da fonte (texto bruto intacto ou HTML bruto sanitizado). |
| `source_extracted.txt` | Conteúdo textual limpo extraído da fonte com data/hora UTC e URL de evidência. |
| `claude_request_sanitized.json` | Payload enviado ao Claude (com segredos ocultados). |
| `claude_response.json` | Resposta estruturada do Claude validada via Pydantic (dados sensíveis mascarados). |
| `normalized_fields.json` | Os 37 campos normalizados após regras de validação, limite de caracteres e conflitos. |
| `field_mapping.json` | Mapeamento entre os rótulos visíveis na página do Stays e os seletores HTML descobertos. |
| `preview.csv` | Planilha de auditoria com `property_id,target_url,field_key,stays_label,current_value,proposed_value,status,confidence,sensitive,action,result,warning`. |
| `execution_report.json` | Relatório consolidado da execução (contagens, divergência 36 vs 37, modo, resultado). |
| `execution.log` | Log detalhado da execução com filtro contra vazamento de segredos. |
| `before_fill.png` | Captura de tela da página do Stays antes do preenchimento. |
| `after_fill.png` | Captura de tela da página do Stays após o preenchimento dos campos aprovados. |
| `error.png` | Captura de tela gerada automaticamente caso ocorra falha em tempo de execução. |

Para inspecionar os artefatos pelo terminal:

```bash
ls -la artifacts/
column -s, -t < artifacts/*_AB25I/preview.csv | less -S
cat artifacts/*_AB25I/execution_report.json
```

---

## 9. Execução com Salvamento (`--save`)

Após validar a execução em `dry-run`, utilize `--save` para habilitar a gravação no Stays. Mesmo com `--save`, a ferramenta:
1. Mostrará a tabela de prévia antes de preencher;
2. Solicitará decisão para qualquer campo que já tenha valor diferente no Stays (`[m]anter`, `[s]ubstituir` ou `[i]gnorar`);
3. Mostrará um painel de **Confirmação Final de Salvamento** após o preenchimento antes de clicar no botão "Salvar".

```bash
uv run python -m stays_automation \
  --target-url "https://ssmt.stays.com.br/i/apartment/AB25I/setup" \
  --source-file "./examples/entrada_exemplo.txt" \
  --save
```

---

## 10. Retomada Após Falha (`--resume`)

O progresso é gravado campo a campo em `progress.json` dentro da pasta da execução. Caso a conexão oscile ou ocorra timeout no navegador, retome a execução apontando `--resume` para a pasta de artefatos gerada; os campos já preenchidos não serão duplicados:

```bash
uv run python -m stays_automation \
  --target-url "https://ssmt.stays.com.br/i/apartment/AB25I/setup" \
  --source-file "./examples/entrada_exemplo.txt" \
  --resume "./artifacts/20261111_170100_AB25I" \
  --dry-run
```

---

## 11. Ajuste de Seletores e Campos Personalizados

- **Campos personalizados (`config/custom_fields.yaml`)**: permite renomear `stays_label`, adicionar `aliases`, ajustar `max_length`, ativar/desativar campos (`enabled: true/false`), exigir formato de URL (`field_type: "url"`) ou marcar sensibilidade (`sensitive: true`).
- **Seletores e Fallbacks (`config/selectors.yaml`)**: caso a interface do Stays sofra alterações de layout, rode `uv run python -m stays_automation inspect --target-url "..."`, verifique os seletores descobertos em `field_mapping.json` e, se necessário, adicione um override explícito em `field_overrides` dentro de `config/selectors.yaml`:

```yaml
field_overrides:
  property_address:
    selector: "textarea[name='custom_field_endereco']"
```

---

## 12. Execução dos Testes Automatizados

A suíte de testes (`pytest`) valida modelos Pydantic, divergência entre 36 e 37 campos, sanitização de segredos, mascaramento de senhas de Wi-Fi/fechaduras, extração de fontes, detecção de conflitos e fluxo completo `dry-run` usando mocks e HTML local (sem tocar no Stays real e sem exigir credenciais reais):

```bash
uv run pytest
```

Ou usando o ambiente virtual `.venv`:

```bash
.venv/bin/pytest
```

---

## 13. Execução Opcional com Docker

O projeto inclui um `Dockerfile` oficial baseado na imagem Linux do Playwright:

```bash
docker build -t stays-content-automation .

docker run --rm -it \
  --env-file .env \
  -v "$(pwd)/artifacts:/app/artifacts" \
  -v "$(pwd)/examples:/app/examples" \
  stays-content-automation \
  --target-url "https://ssmt.stays.com.br/i/apartment/AB25I/setup" \
  --source-file "/app/examples/entrada_exemplo.txt" \
  --headless \
  --dry-run
```

---

## 14. Solução de Problemas Comuns

1. **Erro `Variável de ambiente ANTHROPIC_API_KEY não configurada`**:
   - Verifique se o arquivo `.env` existe na raiz (`cp .env.example .env`) e se contém uma chave válida `ANTHROPIC_API_KEY=sk-ant-...`.
2. **CAPTCHA ou 2FA/MFA no login do Stays**:
   - Execute com `--no-headless` (ou `HEADLESS=false` no `.env`). Quando o terminal solicitar, resolva o desafio manualmente na janela do Chromium e pressione `ENTER` no terminal. A sessão autenticada será salva em `.session/stays_state.json` (com permissão `0600`) para reutilização segura nas próximas execuções.
3. **Campo não localizado na página do Stays**:
   - Execute o modo `inspect` (`uv run python -m stays_automation inspect --target-url "..."`) e consulte `artifacts/<execucao>/field_mapping.json` e `sanitized_page.html` para verificar o rótulo exato exibido na sua conta do Stays e adicioná-lo em `aliases` (`config/custom_fields.yaml`) ou `field_overrides` (`config/selectors.yaml`).
