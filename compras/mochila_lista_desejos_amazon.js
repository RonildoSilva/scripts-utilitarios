/*
 * Mochila na Lista de Desejos da Amazon
 * =====================================
 *
 * Escolhe quais itens da sua lista de desejos comprar sem passar de um
 * orçamento (por exemplo, um bônus educacional) e destaca os escolhidos na
 * própria página.
 *
 * Por baixo é o problema da mochila 0-1: cada item entra ou não entra, a
 * "capacidade" é o orçamento e o "peso" de cada item é o preço.
 *
 * Como usar
 * ---------
 *   1. Abra a lista de desejos e role até o fim. A Amazon carrega a lista aos
 *      poucos, e item que não apareceu na tela não entra na conta.
 *   2. Ajuste a CONFIGURAÇÃO logo abaixo.
 *   3. Abra o console do navegador (F12 > Console), cole o script inteiro e
 *      aperte Enter. Pode rodar de novo à vontade; o destaque anterior é limpo.
 *
 * Detalhes
 * --------
 *   - As contas são feitas em centavos: R$ 49,90 conta como 49,90, sem
 *     arredondar.
 *   - Itens indisponíveis (sem preço) ficam de fora e são listados no final.
 */

(() => {
  // ===================================================== CONFIGURAÇÃO

  /** Quanto você pode gastar, em reais. */
  const ORCAMENTO = 160.0;

  /**
   * O que a escolha deve priorizar:
   *   'gasto'      -> gastar o mais perto possível do orçamento.
   *   'quantidade' -> levar o maior número de itens; entre combinações com o
   *                   mesmo número de itens, fica a que gasta mais.
   */
  const OBJETIVO = 'gasto';

  // ============================================ ALGORITMO DA MOCHILA

  /**
   * Resolve a mochila 0-1 por programação dinâmica.
   *
   * @param {{centavos: number}[]} itens  preço de cada item, em centavos
   * @param {number} capacidade           orçamento, em centavos
   * @param {'gasto'|'quantidade'} objetivo
   * @returns {number[]} índices (em `itens`) dos itens escolhidos
   */
  function resolverMochila(itens, capacidade, objetivo) {
    // Quanto cada item "vale" para o objetivo escolhido.
    //   - 'gasto': vale o próprio preço, então maximizar o valor é maximizar
    //     o gasto.
    //   - 'quantidade': vale (capacidade + 1) + preço. A parcela fixa é maior
    //     que qualquer gasto possível, então um item a mais sempre ganha; o
    //     preço só serve para desempatar.
    const valorDe = (item) =>
      objetivo === 'quantidade' ? capacidade + 1 + item.centavos : item.centavos;

    // melhor[c] = maior valor que dá para obter gastando no máximo c centavos
    //             com os itens vistos até agora.
    const melhor = new Float64Array(capacidade + 1);

    // usou[i][c] = 1 se, ao processar o item i, a melhor solução para o
    //              orçamento c passou a incluir esse item. Serve para
    //              reconstruir a escolha no final.
    const usou = [];

    for (let i = 0; i < itens.length; i++) {
      const preco = itens[i].centavos;
      const valor = valorDe(itens[i]);
      const usouEste = new Uint8Array(capacidade + 1);

      // Percorre o orçamento de trás para frente para que cada item seja
      // usado no máximo uma vez.
      for (let c = capacidade; c >= preco; c--) {
        const comEste = melhor[c - preco] + valor;
        if (comEste > melhor[c]) {
          melhor[c] = comEste;
          usouEste[c] = 1;
        }
      }
      usou.push(usouEste);
    }

    // Reconstrução: parte do orçamento total e volta item a item. Se o item
    // foi usado naquele orçamento, ele entra e o preço dele sai do orçamento.
    const escolhidos = [];
    let restante = capacidade;
    for (let i = itens.length - 1; i >= 0; i--) {
      if (usou[i][restante]) {
        escolhidos.push(i);
        restante -= itens[i].centavos;
      }
    }
    return escolhidos.reverse();
  }

  // Fora do navegador (Node), só exporta o algoritmo para ser testado.
  if (typeof document === 'undefined') {
    module.exports = { resolverMochila };
    return;
  }

  // ================================================ LEITURA DA PÁGINA

  /**
   * Converte um preço em centavos. Devolve null se não houver preço válido.
   *   paraCentavos('49.9')                  -> 4990   (formato do data-price)
   *   paraCentavos('R$ 1.234,56', true)     -> 123456 (formato exibido na tela)
   */
  function paraCentavos(texto, formatoBrasileiro = false) {
    if (texto == null) return null;
    let numero = String(texto).trim();
    if (formatoBrasileiro) {
      numero = numero.replace(/[^\d,]/g, '').replace(',', '.');
    }
    const reais = parseFloat(numero);
    return Number.isFinite(reais) && reais > 0 ? Math.round(reais * 100) : null;
  }

  /**
   * Lê os itens da lista de desejos aberta na página.
   *
   * Cada item é um <li data-itemid>, e nome e preço são lidos de dentro dele.
   * Assim, um item sem preço não desalinha o preço dos itens seguintes.
   *
   * O preço vem do atributo data-price; se ele não existir, do texto do preço
   * exibido. Itens indisponíveis costumam ter data-price="-Infinity".
   */
  function lerItensDaPagina() {
    const comPreco = [];
    const semPreco = [];

    for (const li of document.querySelectorAll('li[data-itemid]')) {
      const link = li.querySelector('a[id^="itemName_"]');
      const nome = (link?.getAttribute('title') || link?.textContent || '(sem nome)').trim();
      const textoPreco = li.querySelector('.a-price .a-offscreen')?.textContent;
      const centavos = paraCentavos(li.dataset.price) ?? paraCentavos(textoPreco, true);

      if (centavos == null) semPreco.push(nome);
      else comPreco.push({ nome, centavos, elemento: li });
    }

    return { comPreco, semPreco };
  }

  // ======================================================= RESULTADO

  const formatarReais = (centavos) =>
    (centavos / 100).toLocaleString('pt-BR', { style: 'currency', currency: 'BRL' });

  /** Tira o destaque de todos os itens e destaca só os escolhidos. */
  function destacarNaPagina(todos, escolhidos) {
    for (const item of todos) {
      item.elemento.style.outline = '';
      item.elemento.style.background = '';
    }
    for (const item of escolhidos) {
      item.elemento.style.outline = '3px solid #e53935';
      item.elemento.style.background = 'rgba(229, 57, 53, 0.08)';
    }
  }

  /** Mostra no console a tabela dos escolhidos e o resumo do orçamento. */
  function imprimirResumo({ comPreco, semPreco, escolhidos, orcamento }) {
    const total = escolhidos.reduce((soma, item) => soma + item.centavos, 0);

    console.log(
      `${comPreco.length + semPreco.length} itens na página, ` +
        `${comPreco.length} com preço. Objetivo: ${OBJETIVO}.`
    );
    console.table(
      escolhidos.map((item) => ({ item: item.nome, preço: formatarReais(item.centavos) }))
    );
    console.log(
      `Escolhidos: ${escolhidos.length} | ` +
        `Total: ${formatarReais(total)} | ` +
        `Orçamento: ${formatarReais(orcamento)} | ` +
        `Sobra: ${formatarReais(orcamento - total)}`
    );
    if (semPreco.length) {
      console.log('Ignorados (sem preço ou indisponíveis):', semPreco);
    }
  }

  // =========================================================== EXECUÇÃO

  const { comPreco, semPreco } = lerItensDaPagina();
  if (comPreco.length + semPreco.length === 0) {
    console.warn('Nenhum item encontrado. Você está na página da lista de desejos?');
    return;
  }

  const orcamento = Math.round(ORCAMENTO * 100);
  const escolhidos = resolverMochila(comPreco, orcamento, OBJETIVO).map((i) => comPreco[i]);

  destacarNaPagina(comPreco, escolhidos);
  imprimirResumo({ comPreco, semPreco, escolhidos, orcamento });
})();
