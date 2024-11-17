import logging
import json
import time
import threading
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import redis

app = Flask(__name__)
CORS(app)

# Configuração do Redis
r = redis.StrictRedis(host='localhost', port=6379, db=0, decode_responses=True)
logging.basicConfig(level=logging.DEBUG)

# Prefixos para facilitar a identificação das chaves no Redis
QUIZ_PREFIX = "quiz:"
USER_PREFIX = "user:"
TIME_PREFIX = "time:"

# Criar índice para RediSearch (se ainda não existir)
def create_search_index():
    try:
        r.execute_command('FT.CREATE', 'idx_votes', 'SCHEMA',
                          'question_id', 'TEXT',
                          'alternative', 'TEXT',
                          'vote_count', 'NUMERIC')
        logging.info("Índice 'idx_votes' criado com sucesso.")
    except redis.exceptions.ResponseError as e:
        logging.info(f"Índice 'idx_votes' já existe: {e}")

create_search_index()  # Criar o índice na inicialização

# Função para obter o tempo atual em segundos
def get_current_time():
    return time.time()

# Função para expurgar respostas antigas a cada 30 dias
def purge_answers():
    current_time = get_current_time()
    for key in r.keys(QUIZ_PREFIX + '*'):
        question_keys = r.keys(key + ":*")
        for question_key in question_keys:
            answered_students = r.smembers(question_key + ":answered")
            for student_id in answered_students:
                response_time = float(r.hget(TIME_PREFIX + question_key + ":response_time", student_id) or 0)
                if current_time - response_time > 30 * 24 * 60 * 60:  # 30 dias
                    r.srem(question_key + ":answered", student_id)
                    r.hdel(TIME_PREFIX + question_key + ":response_time", student_id)
                    logging.info(f"Deleted answer for student {student_id} in question {question_key}")

# Scheduler para rodar a função de purgar as respostas
def run_scheduler():
    while True:
        time.sleep(30 * 24 * 60 * 60)  # Espera 30 dias
        purge_answers()

# Inicia o scheduler em uma thread separada
threading.Thread(target=run_scheduler, daemon=True).start()

# Rota para adicionar usuários
@app.route('/users', methods=['POST'])
def add_users():
    data = request.json
    users = data.get('users', [])

    if not users:
        return jsonify({"error": "At least one user must be provided"}), 400

    added_users = []
    errors = []

    for user in users:
        username = user.get('username')
        user_code = user.get('code')

        if not username or not user_code:
            errors.append(f"User code or username missing for user: {user}")
            continue

        if r.exists(USER_PREFIX + user_code):
            errors.append(f"User code {user_code} already exists.")
            continue

        r.hset(USER_PREFIX + user_code, "username", username)
        added_users.append({"user_code": user_code, "username": username})

    if errors:
        return jsonify({"errors": errors, "added_users": added_users}), 400

    return jsonify({"message": "Users added successfully", "added_users": added_users}), 201

# Rota para pegar todos os usuários
@app.route('/users', methods=['GET'])
def get_users():
    all_users = []
    
    user_keys = r.keys(USER_PREFIX + "*")
    for user_key in user_keys:
        user_code = user_key.split(":")[-1]
        username = r.hget(USER_PREFIX + user_code, "username")
        all_users.append({"user_code": user_code, "username": username})
    
    return jsonify({"users": all_users}), 200

# Rota para criar um quiz
@app.route('/quizzes', methods=['POST'])
def create_quiz():
    data = request.json
    quiz_id = data.get('id')
    questions = data.get('questions')

    if not quiz_id or not questions:
        return jsonify({"error": "Quiz ID and questions are required"}), 400

    if r.exists(QUIZ_PREFIX + quiz_id):
        return jsonify({"error": "Quiz ID already exists"}), 400

    r.hset(QUIZ_PREFIX + quiz_id, "creation_time", get_current_time())

    for question in questions:
        question_id = question['id']
        r.hset(QUIZ_PREFIX + quiz_id + ":" + question_id, mapping={
            "text": question['text'],
            "correct_answer": question['correct_answer'],
            "options": json.dumps(question['options']),
        })

    return jsonify({"message": "Quiz created successfully"}), 201

# Rota para pegar uma questão de um quiz
@app.route('/quizzes/<quiz_id>/questions/<question_id>', methods=['GET'])
def get_question(quiz_id, question_id):
    # Recupera os dados da questão do Redis
    question_data = r.hgetall(QUIZ_PREFIX + quiz_id + ":" + question_id)

    # Verifica se a questão existe
    if not question_data:
        return jsonify({"error": "Question not found"}), 404

    # Verifica se já existe o "start_time" para a questão
    start_time = r.hget(QUIZ_PREFIX + quiz_id + ":" + question_id, "start_time")

    # Se "start_time" não existir, cria um novo timestamp (quando a questão foi acessada pela primeira vez)
    if not start_time:
        start_time = get_current_time()  # Função para obter o timestamp atual
        r.hset(QUIZ_PREFIX + quiz_id + ":" + question_id, "start_time", start_time)

    # Converte as opções da questão de JSON para um objeto Python (lista ou dicionário)
    question_data['options'] = json.loads(question_data['options'])

    # Remover a chave "correct_answer" da resposta
    if 'correct_answer' in question_data:
        del question_data['correct_answer']

    # Adiciona o "start_time" ao dicionário para enviar na resposta
    question_data['start_time'] = start_time

    # Retorna a questão com todas as informações, sem a resposta correta
    return jsonify({
        "question_id": question_id,
        "question": question_data
    }), 200


# Rota para responder a uma questão de um quiz
@app.route('/quizzes/<quiz_id>/answer', methods=['POST'])
def answer_quiz(quiz_id):
    data = request.json
    question_id = data.get('question_id')
    answer = data.get('answer')
    student_id = data.get('student_id')

    if not question_id or not answer or not student_id:
        return jsonify({"error": "Question ID, answer, and student ID are required"}), 400

    # Pega o timestamp real da resposta
    response_timestamp = get_current_time()

    # Verifica se a questão está disponível para responder
    start_time = float(r.hget(QUIZ_PREFIX + quiz_id + ":" + question_id, "start_time") or 0)
    if response_timestamp > start_time + 20:
        return jsonify({"error": "Time expired for answering this question"}), 400

    if r.sismember(QUIZ_PREFIX + quiz_id + ":" + question_id + ":answered", student_id):
        return jsonify({"error": "User has already answered this question"}), 400

    # Calcular o tempo de resposta em segundos
    response_time_in_seconds = response_timestamp - start_time

    # Armazena a resposta e o tempo de resposta
    r.hset(QUIZ_PREFIX + quiz_id + ":" + question_id + ":responses", student_id, answer)
    r.hset(TIME_PREFIX + quiz_id + ":" + question_id + ":response_time", student_id, response_time_in_seconds)

    correct_answer = r.hget(QUIZ_PREFIX + quiz_id + ":" + question_id, "correct_answer")
    if answer == correct_answer:
        r.hincrby(QUIZ_PREFIX + quiz_id + ":correct_answers", student_id, 1)

    r.sadd(QUIZ_PREFIX + quiz_id + ":" + question_id + ":answered", student_id)

    return jsonify({"message": "Answer recorded", "data": data}), 200

# Rota para pegar as respostas de um quiz
@app.route('/quizzes/<quiz_id>/responses', methods=['GET'])
def get_responses_for_quiz(quiz_id):
    """Retorna as respostas enviadas para um quiz específico ou uma questão específica do quiz."""
    question_id = request.args.get('question_id')
    
    quiz_key = QUIZ_PREFIX + quiz_id
    if not r.exists(quiz_key):
        return jsonify({"error": f"Quiz {quiz_id} not found"}), 404

    all_responses = []

    # Verifica se question_id foi fornecido
    if question_id:
        question_key = QUIZ_PREFIX + quiz_id + ":" + question_id
        if not r.exists(question_key):
            return jsonify({"error": f"Question {question_id} not found in quiz {quiz_id}"}), 404
        
        responses = r.hgetall(question_key + ":responses")
        user_keys = r.keys(USER_PREFIX + "*")
        
        for user_key in user_keys:
            user_code = user_key.split(":")[-1]
            student_id = user_code
            answer = responses.get(student_id, "0")
            response_time = r.hget(TIME_PREFIX + quiz_id + ":" + question_id + ":response_time", student_id)
            response_time = float(response_time) if response_time else 20.0
            
            all_responses.append({
                "student_id": student_id,
                "answer": answer,
                "response_time": response_time
            })
    else:
        # Pega todas as respostas do quiz
        question_keys = r.keys(QUIZ_PREFIX + quiz_id + ":*")
        question_keys = [key for key in question_keys if not key.endswith(':responses') and not key.endswith(':correct_answers') and not key.endswith(':answered')]
        
        for question_key in question_keys:
            question_id = question_key.split(":")[-1]
            responses = r.hgetall(QUIZ_PREFIX + quiz_id + ":" + question_id + ":responses")
            
            user_keys = r.keys(USER_PREFIX + "*")
            for user_key in user_keys:
                user_code = user_key.split(":")[-1]
                student_id = user_code
                answer = responses.get(student_id, "0")
                response_time = r.hget(TIME_PREFIX + quiz_id + ":" + question_id + ":response_time", student_id)
                response_time = float(response_time) if response_time else 20.0
                
                all_responses.append({
                    "student_id": student_id,
                    "answer": answer,
                    "response_time": response_time
                })

    return jsonify({
        "quiz_id": quiz_id,
        "responses": all_responses
    }), 200

# Rota para obter as estatísticas (analytics) de uma questão de um quiz
@app.route('/quizzes/<quiz_id>/analytics', methods=['GET'])
def get_quiz_analytics(quiz_id):
    """Retorna as estatísticas de respostas para uma questão específica de um quiz."""

    # Obter o question_id da query string
    question_id = request.args.get('question_id')
    if not question_id:
        return jsonify({"error": "ID da questão é obrigatório"}), 400

    # Verificar se o quiz existe
    quiz_key = QUIZ_PREFIX + quiz_id
    if not r.exists(quiz_key):
        return jsonify({"error": f"Quiz {quiz_id} não encontrado"}), 404

    # Verificar se a questão existe dentro do quiz
    question_key = QUIZ_PREFIX + quiz_id + ":" + question_id
    if not r.exists(question_key):
        return jsonify({"error": f"Questão {question_id} não encontrada no quiz {quiz_id}"}), 404

    # Obter todas as respostas para a questão
    responses = r.hgetall(question_key + ":responses")
    if not responses:
        return jsonify({"error": "Nenhuma resposta encontrada para esta questão"}), 404

    # Contabilizando as respostas
    total_respostas = len(responses)
    acertos = 0
    distribuicao_respostas = {opcao: 0 for opcao in json.loads(r.hget(question_key, "options") or "[]")}
    tempo_total_respostas = 0
    desempenho_alunos = {}  # Armazenará desempenho de cada aluno (acertos, tempo, etc.)

    # Iterar sobre as respostas para calcular as estatísticas
    for student_id, answer in responses.items():
        # Contabilizando as respostas corretas
        resposta_correta = r.hget(question_key, "correct_answer")
        is_correct = 1 if answer == resposta_correta else 0
        acertos += is_correct

        # Atualizar a distribuição de respostas
        if answer in distribuicao_respostas:
            distribuicao_respostas[answer] += 1

        # Contabilizar o tempo de resposta
        tempo_resposta = r.hget(TIME_PREFIX + quiz_id + ":" + question_id + ":response_time", student_id)
        tempo_resposta = float(tempo_resposta) if tempo_resposta else 20.0
        tempo_total_respostas += tempo_resposta

        # Obter o nome do aluno
        username = r.hget(USER_PREFIX + student_id, "username")

        # Armazenar informações do aluno para cálculo de desempenho
        desempenho_alunos[student_id] = {
            'is_correct': is_correct,
            'tempo_resposta': tempo_resposta,
            'username': username  # Incluir o nome do aluno
        }

    # Calcular o tempo médio de resposta
    tempo_medio_resposta = tempo_total_respostas / total_respostas if total_respostas > 0 else 0

    # Cálculo das novas métricas
    # 1. Alternativa mais votada (retornar apenas a mais votada)
    alternativas_mais_votadas = sorted(distribuicao_respostas.items(), key=lambda x: x[1], reverse=True)
    alternativa_mais_votada = alternativas_mais_votadas[0] if alternativas_mais_votadas else None  # Apenas a mais votada

    # 2. Questões com mais abstenções (em relação a total de alunos que poderiam responder)
    total_alunos = len(r.keys(USER_PREFIX + "*"))
    absteve = total_alunos - total_respostas

    # 3. Alunos com maior acerto e mais rápidos
    # Ordenamos os alunos com base no número de acertos e no tempo de resposta
    ranking_alunos = sorted(desempenho_alunos.items(), key=lambda x: (x[1]['is_correct'], -x[1]['tempo_resposta']), reverse=True)
    melhor_aluno = ranking_alunos[0] if ranking_alunos else None  # O melhor aluno (maior acerto e mais rápido)

    # 4. Alunos com maior acerto (sem considerar o tempo de resposta)
    alunos_com_maior_acerto = sorted(desempenho_alunos.items(), key=lambda x: x[1]['is_correct'], reverse=True)
    alunos_com_maior_acerto = [
        {"id": aluno[0], "aluno": aluno[1]['username'], "acertos": aluno[1]['is_correct']} 
        for aluno in alunos_com_maior_acerto if aluno[1]['is_correct'] == 1
    ]  # Só os que acertaram

    # 5. Alunos mais rápidos (sem considerar os acertos)
    alunos_mais_rapidos = sorted(desempenho_alunos.items(), key=lambda x: x[1]['tempo_resposta'])
    melhor_aluno_por_velocidade = alunos_mais_rapidos[0] if alunos_mais_rapidos else None  # O aluno mais rápido

    # Preparar a resposta de analytics
    dados_analytics = {
        "total_respostas": total_respostas,
        "acertos": acertos,
        "erros": total_respostas - acertos,
        "Respostas_mais_votadas": alternativa_mais_votada,
        "tempo_medio_resposta": tempo_medio_resposta,
        "melhor_aluno": {
            "id": melhor_aluno[0] if melhor_aluno else None,
            "Aluno": melhor_aluno[1]['username'] if melhor_aluno else None,
            "acertos": melhor_aluno[1]['is_correct'] if melhor_aluno else None,
            "tempo_resposta": melhor_aluno[1]['tempo_resposta'] if melhor_aluno else None
        },
        "alunos_com_maior_acerto": alunos_com_maior_acerto,
        "melhor_aluno_por_velocidade": {
            "id": melhor_aluno_por_velocidade[0] if melhor_aluno_por_velocidade else None,
            "aluno": melhor_aluno_por_velocidade[1]['username'] if melhor_aluno_por_velocidade else None,
            "tempo_resposta em segundos": melhor_aluno_por_velocidade[1]['tempo_resposta'] if melhor_aluno_por_velocidade else None
        },
        "abstencoes": absteve
    }

    return jsonify({
        "quiz_id": quiz_id,
        "question_id": question_id,
        "analytics": dados_analytics
    }), 200

@app.route('/quizzes/<quiz_id>/ranking', methods=['GET'])
def get_quiz_ranking(quiz_id):
    """Retorna o ranking geral dos alunos de um quiz, considerando todas as questões."""
    
    # Verificar se o quiz existe
    if not quiz_exists(quiz_id):
        return jsonify({"error": f"Quiz {quiz_id} não encontrado"}), 404

    alunos = get_all_students()  # Obter todos os alunos
    desempenho_alunos = initialize_student_performance(alunos)  # Inicializar desempenho dos alunos

    # Obter todas as chaves de perguntas do quiz
    question_keys = get_question_keys(quiz_id)
    
    # Processar as respostas para cada questão
    for question_key in question_keys:
        question_id = extract_question_id(question_key)
        process_respostas_for_question(quiz_id, question_id, alunos, question_key, desempenho_alunos)

    # Calcular o tempo médio de resposta de todos os alunos
    calculate_average_response_time(desempenho_alunos)

    # Ordenar o ranking com base no número de acertos e no tempo de resposta
    ranking = sort_ranking(desempenho_alunos)

    # Retornar o ranking formatado
    return jsonify({
        "quiz_id": quiz_id,
        "ranking": format_ranking(ranking)
    }), 200

# Funções auxiliares
def quiz_exists(quiz_id):
    """Verifica se o quiz existe no banco de dados."""
    quiz_key = QUIZ_PREFIX + quiz_id
    return r.exists(quiz_key)

def get_all_students():
    """Retorna uma lista com todos os alunos cadastrados."""
    alunos = r.keys(USER_PREFIX + "*")
    return [aluno.split(":")[-1] for aluno in alunos]

def initialize_student_performance(alunos):
    """Inicializa o desempenho de todos os alunos."""
    return {aluno: {
                "total_acertos": 0,
                "total_respostas": 0,
                "tempo_total_resposta": 0,
                "tempo_medio_resposta": 0
            } for aluno in alunos}

def get_question_keys(quiz_id):
    """Retorna as chaves de todas as questões de um quiz, excluindo respostas e respostas corretas."""
    question_keys = r.keys(QUIZ_PREFIX + quiz_id + ":*")
    return [key for key in question_keys if not key.endswith(":responses") and not key.endswith(":correct_answers")]

def extract_question_id(question_key):
    """Extrai o ID da questão a partir da chave."""
    return question_key.split(":")[-1]

def process_respostas_for_question(quiz_id, question_id, alunos, question_key, desempenho_alunos):
    """Processa as respostas de todos os alunos para uma questão específica e atualiza o desempenho."""
    respostas = r.hgetall(question_key + ":responses")
    
    for aluno, resposta in respostas.items():
        is_correct = check_answer(question_key, resposta)
        tempo_resposta = calculate_response_time(quiz_id, question_id, aluno, question_key)
        
        if tempo_resposta is not None:  # Considera apenas alunos com tempo de resposta válido
            update_student_performance(aluno, is_correct, tempo_resposta, desempenho_alunos)
    
    # Para os alunos que não responderam à questão, atribui o tempo de resposta padrão (20s).
    for aluno in alunos:
        if aluno not in respostas:
            # Atribui 20 segundos para alunos que não responderam
            desempenho_alunos[aluno]["total_respostas"] += 1
            desempenho_alunos[aluno]["tempo_total_resposta"] += 20.0  # Tempo padrão de 20 segundos

def check_answer(question_key, resposta):
    """Verifica se a resposta está correta."""
    resposta_correta = r.hget(question_key, "correct_answer")
    return resposta == resposta_correta

def calculate_response_time(quiz_id, question_id, aluno, question_key):
    """Calcula o tempo de resposta de um aluno para uma questão, usando a diferença entre os timestamps."""

    # Obter o timestamp de início da questão (quando o GET foi feito)
    start_time_raw = r.hget(question_key, "start_time")
    
    # Verificar se o start_time existe e é válido
    if not start_time_raw:
        logging.error(f"start_time não encontrado para {question_key} | quiz_id: {quiz_id}, question_id: {question_id}")
        return None  # Se start_time não existir, não há como calcular o tempo

    try:
        start_time = float(start_time_raw)
        logging.debug(f"start_time para {question_key}: {start_time}")
    except ValueError:
        logging.error(f"Valor inválido para start_time: {start_time_raw} | quiz_id: {quiz_id}, question_id: {question_id}")
        return None

    # Obter o timestamp de resposta do aluno (quando a resposta foi submetida)
    tempo_resposta_raw = r.hget(TIME_PREFIX + quiz_id + ":" + question_id + ":response_time", aluno)

    # Verificar se o tempo de resposta existe
    if not tempo_resposta_raw:
        return None  # Se o tempo de resposta não existir, retornar None
    
    try:
        tempo_resposta = float(tempo_resposta_raw)
    except (ValueError, TypeError):
        return None

    # Calcular o tempo real de resposta (diferença entre timestamp_resposta e timestamp_start)
    tempo_real_resposta = tempo_resposta 

    # Log da diferença do tempo
    logging.debug(f"Diferença entre tempo_resposta e start_time para aluno {aluno}: {tempo_real_resposta}")

    # Verificar se o tempo real de resposta é válido
    if tempo_real_resposta <= 0:
        return None  # Se o tempo for inválido (zero ou negativo), retornamos None

    return tempo_real_resposta

def update_student_performance(aluno, is_correct, tempo_real_resposta, desempenho_alunos):
    """Atualiza o desempenho de um aluno com base na resposta e no tempo de resposta."""
    if is_correct:
        desempenho_alunos[aluno]["total_acertos"] += 1
    desempenho_alunos[aluno]["total_respostas"] += 1
    desempenho_alunos[aluno]["tempo_total_resposta"] += tempo_real_resposta

def calculate_average_response_time(desempenho_alunos):
    """Calcula o tempo médio de resposta de todos os alunos."""
    for aluno in desempenho_alunos:
        total_respostas = desempenho_alunos[aluno]["total_respostas"]
        if total_respostas > 0:
            desempenho_alunos[aluno]["tempo_medio_resposta"] = desempenho_alunos[aluno]["tempo_total_resposta"] / total_respostas
        else:
            desempenho_alunos[aluno]["tempo_medio_resposta"] = 0

def sort_ranking(desempenho_alunos):
    """Ordena os alunos pelo número de acertos (decrescente) e pelo tempo de resposta (crescente)."""
    return sorted(desempenho_alunos.items(), key=lambda x: (-x[1]["total_acertos"], x[1]["tempo_medio_resposta"]))


def format_ranking(ranking):
    """Formata o ranking para exibição na resposta."""
    ranking_formatado = []
    for i, (aluno_id, dados) in enumerate(ranking, start=1):
        nome_aluno = r.hget(USER_PREFIX + aluno_id, "username")
        ranking_formatado.append({
            "posicao": i,
            "student_id": aluno_id,
            "nome": nome_aluno,
            "acertos": dados["total_acertos"],
            "tempo_medio_resposta": round(dados["tempo_medio_resposta"], 2)
        })
    return ranking_formatado

if __name__ == '__main__':
    app.run(debug=True, port=5001)
