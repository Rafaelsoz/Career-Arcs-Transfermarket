# Scraper de Arcos de Carreira no Transfermarkt
Este projeto contém o script `transfermarkt_scraping.py`, usado para coletar dados de jogadores no Transfermarkt e gerar arquivos `.csv` para análise de arcos de carreira.

O script percorre competições, temporadas, elencos e perfis de jogadores e salva informações de:

- vínculo do jogador com clubes por temporada;
- perfil do atleta;
- histórico de valor de mercado;
- lesões;
- transferências;
- resumo de desempenho por temporada;
- base consolidada para análise (`career_arcs_base.csv`).

## Requisitos
- Python 3.10+;
- acesso à internet;
- dependências do arquivo `requirements.txt`.

Instalação:
```bash
pip install -r requirements.txt
```

---
---

# Como executar
Execução padrão:
```bash
python transfermarkt_scraping.py
```

Por padrão, o script:

- coleta dados das competições `BRA1` e `BRA2`;
- percorre `display_year` de `2018` até `2026`;
- salva a saída em `data_transfermarkt_arcos`;
- usa `4` workers na etapa de coleta por jogador;
- espera entre `1.0` e `2.0` segundos entre requisições.

## Exemplos de uso
Executar um teste pequeno:
```bash
python transfermarkt_scraping.py \
  --competitions BRA1 \
  --display-start 2024 \
  --display-end 2024 \
  --max-players 10 \
  --workers 1 \
  --out data_transfermarkt_arcos_test
```

Filtrar o conjunto final para jogadores brasileiros:
```bash
python transfermarkt_scraping.py \
  --nationality-filter Brasil \
  --out data_transfermarkt_arcos_br
```

Executar com logs detalhados:
```bash
python transfermarkt_scraping.py --log-level DEBUG
```

## Parâmetros disponíveis
```bash
python transfermarkt_scraping.py --help
```
Argumentos:
- `--display-start`: ano inicial exibido no loop de temporadas. Padrão: `2018`.
- `--display-end`: ano final exibido no loop de temporadas. Padrão: `2026`.
- `--competitions`: lista de competições. Padrão: `BRA1 BRA2`.
- `--nationality-filter`: filtra os arquivos finais pela nacionalidade do jogador. Ex.: `Brasil`.
- `--out`: diretório de saída. Padrão: `data_transfermarkt_arcos`.
- `--min-sleep`: pausa mínima entre requisições HTTP. Padrão: `1.0`.
- `--max-sleep`: pausa máxima entre requisições HTTP. Padrão: `2.0`.
- `--max-players`: interrompe a coleta após atingir um número máximo de jogadores únicos.
- `--workers`: quantidade de workers paralelos na etapa por jogador. Padrão: `4`.
- `--log-level`: nível de log no console. Opções: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`.

## Competições suportadas
No código atual, as competições mapeadas são:

- `BRA1`: Campeonato Brasileiro Série A
- `BRA2`: Campeonato Brasileiro Série B

Se você passar outros códigos sem antes alterar o dicionário `COMPETITION_SLUGS`, o script não saberá montar a URL corretamente.

---
---

# Estrutura da saída
Dentro da pasta definida em `--out`, o script gera:

- `squad_membership.csv`: relação jogador-clube-temporada encontrada nos elencos.
- `profiles.csv`: dados cadastrais e contratuais do jogador.
- `market_values.csv`: histórico de valor de mercado por data.
- `injuries.csv`: histórico de lesões.
- `transfers.csv`: histórico de transferências.
- `performance_summaries.csv`: resumo de desempenho por temporada.
- `career_arcs_base.csv`: base consolidada com perfil + valores de mercado + idade estimada.
- `cache/`: HTMLs salvos para reaproveitar requisições.
- `debug/`: páginas salvas quando o histórico de valor de mercado não pôde ser extraído.

## Colunas principais
`squad_membership.csv`
- `competition_code`, `display_year`, `tm_season_id`, `season_label`
- `club_id`, `club_slug`, `club_name_guess`
- `player_id`, `player_slug`, `profile_url`, `squad_url`

`profiles.csv`
- `player_id`, `player_slug`, `player_name`, `profile_url`
- `full_name`, `birth_date`, `age_current`, `birth_place`, `nationality`
- `height_text`, `position_group`, `position_detail`, `preferred_foot`
- `agent`, `current_club`, `club_since`, `contract_until`, `last_contract_extension`
- `current_market_value_eur`, `current_market_value_date`

`market_values.csv`
- `player_id`, `player_slug`, `valuation_date`, `market_value_eur`
- `club_at_valuation`, `source_pattern`

`injuries.csv`
- `season_label`, `injury`, `from_date`, `to_date`
- `days_out`, `games_missed`, `player_id`, `player_slug`

`transfers.csv`
- sempre contém `player_id` e `player_slug`;
- as demais colunas dependem da tabela identificada no HTML do Transfermarkt;
- quando presentes, colunas de data são convertidas para data e colunas monetárias também ganham versões com sufixo `_eur`.

`performance_summaries.csv`
- `player_id`, `player_slug`, `tm_season_id`, `season_label`
- `summary_row_text`, `metric_*`
- `minutes_est`, `appearances_est`

`career_arcs_base.csv`
- herda dados de `market_values.csv`;
- agrega colunas de perfil como `player_name`, `full_name`, `birth_date`, `nationality`, `position_group` e `position_detail`;
- calcula `age_years` na data de cada avaliação de mercado.

## Observações importantes
- O script depende da estrutura HTML atual do Transfermarkt. Se o site mudar, alguma etapa pode parar de extrair dados corretamente.
- O filtro `--nationality-filter` é aplicado no fim da coleta. Ou seja, ele reduz os arquivos finais, mas não evita a raspagem anterior.
- Alguns arquivos podem sair vazios se uma etapa não encontrar dados.
- A pasta `cache/` evita baixar novamente páginas já coletadas no mesmo diretório de saída.
- O diretório `debug/` só aparece quando a etapa de valor de mercado falha para algum jogador.

## Interpretação das temporadas
O script trabalha com dois conceitos:
- `display_year`: ano usado no loop de execução;
- `tm_season_id`: ano-base da temporada no Transfermarkt.

Exemplo:
- `display_year = 2018`
- `tm_season_id = 2017`
- `season_label = 17/18`

---
---