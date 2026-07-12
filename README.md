# ATLAS

Sistema de gestão de pendências da implantação FICO.

## Estado atual

Esta versão possui:

- interface responsiva com identidade visual FICO;
- autenticação local com PBKDF2-SHA256;
- sessões com expiração de 12 horas;
- perfis Administrador, Gestor FICO, Fiscal FICO, Contratada e Consulta;
- autorização de baixa por especialidade ou aprovação geral;
- segregação de dados da contratada por empresa;
- cadastro de usuários com troca obrigatória de senha;
- pendências, evidências, correções, decisões e histórico;
- trilha de auditoria;
- dashboard gerencial;
- importador idempotente da base Excel;
- esquema equivalente para PostgreSQL.
- operação offline com IndexedDB e service worker;
- fila automática de abertura, correção e decisão;
- sincronização idempotente após reconexão.

## Uso em campo sem internet

Antes de sair para campo, o usuário deve entrar no ATLAS conectado e selecionar **Preparar offline**. O sistema grava no dispositivo as pendências permitidas para o perfil e as evidências disponíveis.

Sem conexão, o ATLAS permite:

- consultar a carteira previamente preparada;
- abrir pendências com foto;
- informar correções com foto;
- registrar aprovação ou rejeição, conforme a alçada do fiscal.

As ações ficam na fila local do dispositivo. O indicador no topo mostra `Offline`, `aguardando envio` ou `Sincronizado`. Quando a internet retorna, o ATLAS envia a fila automaticamente. Cada operação recebe um identificador único, e o servidor impede gravações duplicadas mesmo se uma transmissão for repetida.

O acesso offline é provisionado após um login online válido e permanece disponível por até sete dias no dispositivo. Após esse prazo, uma conexão é exigida para renovar a autorização. Senhas não são armazenadas; o dispositivo mantém apenas um verificador PBKDF2.

Cadastros administrativos de usuários e alterações de segurança continuam exigindo conexão.

## Como iniciar

Execute `start_atlas.cmd` e abra:

`http://127.0.0.1:8000`

Credencial inicial de homologação:

- E-mail: `thyago.viegas@vale.com`
- Senha temporária: `Atlas@2026`

A senha inicial deve ser trocada antes de uma implantação compartilhada.

## Banco de homologação

O ambiente local usa SQLite em `backend/data/atlas.db`. Essa escolha permite executar a homologação sem instalar serviços adicionais. O arquivo `backend/postgresql_schema.sql` contém o modelo de produção equivalente.

O diretório `backend` não é exposto pelo servidor HTTP. Evidências são disponibilizadas somente pela rota controlada `/uploads/`.

## Importação do Excel

Validação sem gravar:

```powershell
& 'C:\Users\engtv\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' '.\backend\import_excel.py'
```

Aplicação dos registros válidos:

```powershell
& 'C:\Users\engtv\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' '.\backend\import_excel.py' --apply
```

O importador preserva `Id_Pendencia` em `source_id`, não duplica registros e grava o resultado em `backend/data/import-report.json`.

## Fotos das pendências existentes

O administrador pode anexar uma foto individual ao editar qualquer pendência. Para grandes volumes, use `backend/import_photos.py`.

Organize as fotos em uma pasta usando o ID original da planilha no início do nome:

- `125_abertura_01.jpg`
- `125_abertura_02.jpg`
- `125_correcao_01.jpg`
- `125_documento_01.png`

Também é aceito `125.jpg`, tratado como foto de abertura. Os tipos reconhecidos são `abertura`, `correcao` e `documento`. Formatos aceitos: JPEG, PNG e WebP.

Primeiro execute a validação sem gravar:

```powershell
& 'C:\Users\engtv\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' '.\backend\import_photos.py' 'C:\CAMINHO\PARA\FOTOS'
```

Depois confira `backend/data/photo-import-report.json` e aplique:

```powershell
& 'C:\Users\engtv\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' '.\backend\import_photos.py' 'C:\CAMINHO\PARA\FOTOS' --apply
```

A carga é repetível: arquivos já associados ao mesmo ID, tipo e nome são ignorados.

## Variáveis de ambiente

- `ATLAS_ADMIN_PASSWORD`: senha usada somente na criação inicial do administrador.
- `ATLAS_HOST`: interface de rede, padrão `127.0.0.1`.
- `ATLAS_PORT`: porta HTTP, padrão `8000`.
- `DATABASE_URL`: conexão PostgreSQL; quando definida, substitui o SQLite local.
- `SUPABASE_URL`: URL pública do projeto Supabase.
- `SUPABASE_SERVICE_ROLE_KEY`: chave secreta usada somente pelo servidor.
- `SUPABASE_BUCKET`: bucket privado de evidências, padrão `evidencias`.

No primeiro início com um PostgreSQL vazio, o ATLAS cria o esquema e migra automaticamente as 1.895 pendências homologadas de `backend/data/atlas.db`. A carga é executada somente quando a tabela de pendências está vazia.

Para disponibilizar o sistema na rede, a implantação deve usar HTTPS, PostgreSQL, backup, política de retenção das fotos e um proxy corporativo.

## Estrutura

- `index.html`, `styles.css`, `app.js`: aplicação web.
- `assets/brand`: ativos oficiais FICO.
- `backend/server.py`: servidor e API.
- `backend/schema.sql`: esquema SQLite de homologação.
- `backend/postgresql_schema.sql`: esquema PostgreSQL de produção.
- `backend/import_excel.py`: migração da base unificada.
