# As Ideias por Trás da Coleta de Dados
Este documento complementa o README. O README descreve **como operar** o scraper — instalação, comandos, parâmetros e arquivos gerados; aqui o foco são as **decisões conceituais** que sustentam uma coleta confiável e em escala.

O desafio não é apenas "ler uma página": é reconstruir a trajetória de centenas de jogadores a partir de dados fragmentados, distribuídos por milhares de páginas e em mais de uma fonte, sem sobrecarregar o site e tolerando as falhas inevitáveis de uma operação longa. Cada conceito a seguir responde a uma dessas dificuldades.

## Descoberta progressiva em camadas
A coleta não parte de uma lista pronta de jogadores — ela a **constrói** percorrendo a estrutura do site do geral ao específico:

- de uma **competição** em determinada temporada, derivam-se os **times**;
- de cada time, chega-se ao **elenco** e aos jogadores;
- de cada jogador, ao seu **perfil** e às informações associadas.

O princípio é que **cada nível fornece os endereços do nível seguinte**. Em vez de depender de um catálogo fixo, o scraper expande a fronteira de coleta dinamicamente, o que mantém o alcance correto mesmo quando elencos e participantes mudam de uma temporada para outra.

## Duas fontes e estratégia de fallback
A mesma informação pode vir de origens distintas, com qualidades diferentes:

- uma **API de dados**, que devolve conteúdo já estruturado e limpo;
- o **HTML renderizado**, a página tal como exibida ao usuário.

A API é a fonte preferencial, por entregar dados consistentes e fáceis de tratar. Quando ela está indisponível ou incompleta, a coleta recai sobre o HTML — uma **estratégia de fallback** que troca a fonte ideal por uma alternativa resiliente. Manter mais de um caminho para o mesmo dado reduz a perda de informação diante de instabilidades pontuais.

## Extração tolerante a variações de formato
O conteúdo de uma página vem entremeado de navegação, marcação e texto livre, e o mesmo dado pode aparecer em formatos diferentes conforme a página. A extração consiste em **localizar a informação relevante por padrões estáveis** — a posição de um campo no perfil, a estrutura de uma tabela, a assinatura de uma data ou de um valor monetário.

Para absorver essa heterogeneidade, a coleta combina **mais de uma estratégia de reconhecimento** e converte o resultado para um **esquema único e padronizado**. Assim, registros vindos de páginas com pequenas diferenças de layout chegam ao final comparáveis entre si.

## Coleta responsável e tolerância a falhas
Uma operação automatizada e prolongada precisa ser sustentável tanto para o site de origem quanto para si mesma. Três princípios orientam esse comportamento:

- **transparência de cliente** — o scraper se apresenta como um agente legítimo, em vez de mascarar sua natureza;
- **controle de cadência** (*rate limiting*) — os acessos são espaçados para não saturar o servidor nem disparar bloqueios por excesso de requisições;
- **tolerância a falhas** — diante de erros transitórios de rede, a coleta aguarda e **repete a tentativa** de forma escalonada, em vez de abortar no primeiro contratempo.

O efeito combinado é uma coleta **estável**, capaz de atravessar horas de execução sem se romper a cada instabilidade.

## Reaproveitamento e idempotência
Repercorrer milhares de páginas a cada execução seria oneroso e desnecessário. O scraper mantém um **registro do que já foi obtido** e o reutiliza nas execuções seguintes, em vez de requisitar novamente o mesmo conteúdo.

Esse reaproveitamento reduz o tempo total e o número de acessos ao site, e torna a coleta **idempotente** na prática: reexecuções tendem a partir de onde pararam, retomando o trabalho sem refazê-lo do zero.

## Concorrência controlada
A etapa por jogador é a mais custosa, por envolver muitos acessos independentes. Em vez de processá-los em série, a coleta os executa em **concorrência**, tratando vários jogadores ao mesmo tempo e elevando substancialmente a vazão.

Esse paralelismo é **deliberadamente limitado**: precisa coexistir com o controle de cadência da seção anterior. O equilíbrio buscado é ganhar desempenho sem violar a coleta responsável — rápido o bastante para ser viável, contido o bastante para não pressionar o servidor.

## Integração das dimensões
Cada jogador é descrito por **múltiplas dimensões** — identidade, valor de mercado ao longo do tempo, lesões, transferências e desempenho. Tratadas isoladamente, têm valor analítico limitado.

A etapa final **integra essas dimensões em torno de uma mesma identidade**, produzindo uma visão longitudinal da trajetória. É essa integração que viabiliza o objetivo do projeto: analisar **arcos de carreira** — ascensão, auge e declínio ao longo dos anos — em vez de observações isoladas no tempo.
