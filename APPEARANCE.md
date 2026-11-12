# Aparência Sal0

O botão **Temas**, ao lado do alternador claro/escuro, oferece Cristal,
Aurora, Wall e **Tema antigo**. Esta última opção usa os estilos originais
do próprio aplicativo nos dois modos de cor. A escolha fica neste navegador.

Os temas novos usam cartões translúcidos e ondas azuis contínuas. O fundo usa
a maior altura estável da janela; a rolagem e a barra do navegador não
redimensionam as ondas nem reiniciam a animação. Navegadores antigos medem
o tamanho novamente somente após uma mudança real de largura/orientação.

O controle de movimento, a preferência de movimento reduzido do aparelho,
o tema antigo e uma aba oculta pausam as ondas. Não há eventos de rolagem
ou acesso aos serviços do aplicativo no código de aparência.

O modo claro solicita `color-scheme: only light`; o escuro declara `dark`.
A metatag `darkreader-lock` solicita a preservação dos temas no Dark Reader.
Os clientes Android também desativam Force Dark e escurecimento algorítmico
no WebView. Filtros externos que ignorem essas opções podem interferir.

Os controles, APIs, filas, autenticação e processamento continuam usando
a implementação existente do aplicativo.
