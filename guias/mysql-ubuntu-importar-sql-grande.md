# Guia Completo: Instalação, Configuração e Importação de Arquivos SQL Gigantes no MySQL (Ubuntu)

Este guia cobre o processo de ponta a ponta para instalar o MySQL no Ubuntu, criar usuários para acesso em ferramentas gráficas (como o DBeaver) e importar arquivos de banco de dados (`.sql`) extremamente grandes (ex: 5GB+) sem travar o sistema.

---

## 1. Instalação e Preparação do MySQL

Se você está começando do zero, instale o servidor do MySQL e garanta que ele esteja rodando perfeitamente.

Abra o terminal e execute:

```bash
# Atualiza a lista de pacotes
sudo apt update

# Instala o servidor MySQL
sudo apt install mysql-server -y

# Força o serviço a iniciar e verifica o status
sudo systemctl start mysql
sudo systemctl status mysql
```
*(No status, procure pela indicação **"active (running)"** em verde. Aperte a letra `q` para sair dessa tela).*

---

## 2. Criação do Banco de Dados Base

Com o MySQL rodando, precisamos criar o banco de dados vazio que irá receber os dados do arquivo `.sql`.

```bash
# Acessa o MySQL como administrador (root) e cria o banco
sudo mysql -u root -e "CREATE DATABASE nome_do_banco;"
```
*(Substitua `nome_do_banco` pelo nome desejado, sem espaços).*

---

## 3. Importação de Arquivos SQL Gigantes

**Regra de Ouro:** Nunca tente abrir um arquivo `.sql` de vários Gigabytes em ferramentas visuais (DBeaver, Workbench). O seu computador tentará carregar tudo na memória RAM e irá travar. O terminal é a única via segura.

Para importar, usaremos um parâmetro especial para evitar que o MySQL rejeite pacotes de texto muito longos durante a leitura.

```bash
sudo mysql -u root --max_allowed_packet=1G nome_do_banco < /caminho/completo/para/o/arquivo.sql
```

**⚠️ Atenção:**
* O sinal `<` faz com que o arquivo seja "injetado" direto no banco.
* **O terminal vai parecer travado!** Ele não exibirá barra de progresso. Para arquivos de 5GB+, isso pode demorar de 10 a 30 minutos (ou mais, dependendo do seu HD/SSD).
* O processo só termina quando o terminal liberar o cursor para você digitar novamente.

---

## 4. Liberação de Acesso para o DBeaver (Criação de Usuário)

O Ubuntu bloqueia o acesso remoto/gráfico ao usuário `root` por padrão. Para usar o DBeaver, precisamos criar um usuário dedicado com senha.

Acesse o painel do MySQL no terminal:

```bash
sudo mysql
```

Dentro do prompt do MySQL (`mysql>`), execute os comandos abaixo (substitua o usuário e senha como preferir):

```sql
-- 1. Cria o usuário com senha
CREATE USER 'seu_usuario'@'localhost' IDENTIFIED BY 'sua_senha';

-- 2. Dá permissão total a esse usuário em todos os bancos
GRANT ALL PRIVILEGES ON *.* TO 'seu_usuario'@'localhost';

-- 3. Aplica as regras e sai do painel
FLUSH PRIVILEGES;
EXIT;
```

---

## 5. Configuração no DBeaver e Solução de Erro de Chave

Agora você pode conectar no DBeaver com os dados criados. 

### Dados da Conexão:
* **Server Host:** `localhost`
* **Database:** `nome_do_banco` (ou deixe em branco)
* **Username:** `seu_usuario`
* **Password:** `sua_senha`

### 🔧 Solução de Erro Comum (Public Key Retrieval is not allowed)
Nas versões mais recentes do MySQL, o DBeaver pode bloquear a conexão por questões de certificado de segurança. Para corrigir isso em conexões locais:

1. No DBeaver, clique com o botão direito na sua conexão e vá em **Edit Connection** (Editar Conexão).
2. No menu lateral esquerdo, clique em **Driver Properties** (Propriedades do Driver).
3. Na lista de propriedades, procure por `allowPublicKeyRetrieval`.
4. Mude o valor ao lado dela de `FALSE` para **`TRUE`**.
5. Clique em **Test Connection** para validar e depois em **OK** para salvar.

Pronto! Seu ambiente está configurado e seus dados gigantescos estão prontos para serem explorados no DBeaver.