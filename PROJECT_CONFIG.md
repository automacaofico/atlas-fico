# ATLAS

## Configuração inicial aprovada

- Administrador inicial: Thyago Pinheiro Viégas Mendonça
- E-mail: thyago.viegas@vale.com
- Domínio corporativo principal: vale.com
- Capacidade inicial: 50 usuários FICO e 50 usuários de contratadas
- Autenticação inicial: usuário local do ATLAS
- Autocadastro: desabilitado
- Aprovisionamento: somente por administrador autorizado

## Diretriz de identidade

O primeiro administrador utilizará autenticação local, sem integração Microsoft. Uma futura integração com Microsoft Entra ID poderá ser adicionada sem alterar os perfis e permissões do ATLAS.

O perfil e a autorização de negócio serão mantidos no banco do ATLAS, mesmo quando a autenticação for realizada pela Microsoft. Isso inclui empresa, perfil, especialidades de fiscalização, aprovação geral, pacotes, segmentos e status ativo.

## Escala inicial

A primeira implantação deve suportar pelo menos 100 usuários cadastrados, sem assumir 100 acessos simultâneos. O dimensionamento poderá ser ampliado sem mudança no modelo de permissões.
