"""Criticidade e planejamento: cálculos puros, sem execução de pagamentos."""

from decimal import Decimal

LEVELS = {5: 'Extrema', 4: 'Alta', 3: 'Moderada', 2: 'Baixa', 1: 'Não crítica'}
AGE_LABELS = {
    5: '>120 dias',
    4: '91–120 dias',
    3: '61–90 dias',
    2: '31–60 dias',
    1: '1–30 dias',
}
CRITICAL_WEIGHTS = {5: 40, 4: 30, 3: 20, 2: 7, 1: 3}
AGE_WEIGHTS = {5: 30, 4: 25, 3: 20, 2: 15, 1: 10}
# Cores por coordenada da especificação 1.3; não são produto dos níveis.
COLORS = (
    ('red', 'red', 'red', 'red', 'red'),
    ('red', 'yellow', 'yellow', 'yellow', 'green'),
    ('yellow', 'blue', 'blue', 'blue', 'neutral'),
    ('green', 'green', 'green', 'green', 'neutral'),
    ('neutral', 'neutral', 'neutral', 'neutral', 'neutral'),
)


def age_level(days):
    if days <= 0:
        return 0
    return next(
        (
            level
            for threshold, level in ((120, 5), (90, 4), (60, 3), (30, 2))
            if days > threshold
        ),
        1,
    )


def classification(treatment):
    level = treatment.get('nivel_criticidade')
    if level is None and not treatment.get('critico', False):
        level = 1
    return {
        'nivel_criticidade': level,
        'criticidade_label': LEVELS.get(level, 'Crítico · revisar nível'),
        'criticidade_pendente': level is None,
        'justificativa_criticidade': treatment.get('justificativa_criticidade')
        or '',
        'data_classificacao': treatment.get('data_classificacao'),
        'classificado_por': treatment.get('classificado_por'),
    }


def allocate_budget(amount):
    """Distribui centavos pelo maior resto; a soma sempre é o orçamento."""
    cents = int(Decimal(str(amount)).quantize(Decimal('0.01')) * 100)
    if cents < 0:
        raise ValueError('Orçamento não pode ser negativo.')
    weights = [
        (c, a, CRITICAL_WEIGHTS[c] * AGE_WEIGHTS[a])
        for c in LEVELS
        for a in AGE_LABELS
    ]
    allocations = {(c, a): cents * weight // 10000 for c, a, weight in weights}
    remaining = cents - sum(allocations.values())
    ranked = sorted(weights, key=lambda item: -(cents * item[2] % 10000))
    for c, a, _ in ranked[:remaining]:
        allocations[c, a] += 1
    return {key: Decimal(value) / 100 for key, value in allocations.items()}


def build_matrix(suppliers, amount):
    budgets = allocate_budget(amount)
    cells = {}
    for c in LEVELS:
        for a in AGE_LABELS:
            cells[c, a] = {
                'nivel': c,
                'faixa': a,
                'quadrante': (5 - c) * 5 + (6 - a),
                'cor': COLORS[5 - c][5 - a],
                'quantidade': 0,
                'valor': Decimal('0'),
                'orcamento': budgets[c, a],
            }
    pending = {'quantidade': 0, 'valor': Decimal('0')}
    for supplier in suppliers:
        level = supplier['nivel_criticidade']
        for title in supplier['titulos']:
            age = age_level(title['dias_vencidos'])
            if not age or title['saldo_a_pagar'] <= 0:
                continue
            cell = cells[level, age] if level in LEVELS else pending
            cell['quantidade'] += 1
            cell['valor'] += title['saldo_a_pagar']
    rows = []
    for c, label in LEVELS.items():
        row_cells = [cells[c, a] for a in AGE_LABELS]
        for cell in row_cells:
            cell['cobertura'] = min(cell['valor'], cell['orcamento'])
            cell['saldo_sem_cobertura'] = max(
                cell['valor'] - cell['orcamento'], Decimal('0')
            )
            cell['sobra'] = max(
                cell['orcamento'] - cell['valor'], Decimal('0')
            )
        rows.append({
            'nivel': c,
            'label': label,
            'percentual': CRITICAL_WEIGHTS[c],
            'celulas': row_cells,
            'quantidade': sum(cell['quantidade'] for cell in row_cells),
            'valor': sum((cell['valor'] for cell in row_cells), Decimal('0')),
            'orcamento': sum(
                (cell['orcamento'] for cell in row_cells), Decimal('0')
            ),
        })
    columns = [
        {
            'faixa': a,
            'label': label,
            'percentual': AGE_WEIGHTS[a],
            'quantidade': sum(cells[c, a]['quantidade'] for c in LEVELS),
            'valor': sum((cells[c, a]['valor'] for c in LEVELS), Decimal('0')),
            'orcamento': sum(
                (cells[c, a]['orcamento'] for c in LEVELS), Decimal('0')
            ),
        }
        for a, label in AGE_LABELS.items()
    ]
    return {
        'linhas': rows,
        'colunas': columns,
        'pendentes': pending,
        'quantidade': sum(row['quantidade'] for row in rows),
        'valor': sum((row['valor'] for row in rows), Decimal('0')),
        'orcamento': sum(budgets.values(), Decimal('0')),
        'sobra': sum((cell['sobra'] for cell in cells.values()), Decimal('0')),
        'cobertura': sum(
            (cell['cobertura'] for cell in cells.values()), Decimal('0')
        ),
    }
