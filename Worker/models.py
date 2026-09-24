"""
Independent models for Worker
Mirrors the backend models for tables the Worker needs access to
"""
from sqlalchemy import Column, Integer, String, Date, DateTime, ForeignKey, Text, BigInteger
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base


class Carteirinha(Base):
    __tablename__ = "carteirinhas"

    id = Column(Integer, primary_key=True, index=True)
    carteirinha = Column(Text, unique=True, nullable=False)
    paciente = Column(Text)
    id_paciente = Column(Integer, index=True)
    id_pagamento = Column(Integer, index=True)
    status = Column(Text, default="ativo")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    jobs = relationship("Job", back_populates="carteirinha_rel")
    guias = relationship("BaseGuia", back_populates="carteirinha_rel")
    logs = relationship("Log", back_populates="carteirinha_rel")


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, index=True)
    carteirinha_id = Column(Integer, ForeignKey("carteirinhas.id", ondelete="CASCADE"))
    status = Column(Text, nullable=False, default="pending")  # success, pending, processing, error
    rotina = Column(Text, nullable=True, index=True)     # ex: 'clmf_atualizar_rc'
    params = Column(JSONB, nullable=True)                # parâmetros arbitrários do job
    attempts = Column(Integer, default=0)
    priority = Column(Integer, default=0)
    locked_by = Column(Text)  # Server URL
    timeout = Column(DateTime(timezone=True))
    valida_prestador = Column(JSONB, nullable=True)  # JSON de validacao do prestador
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    carteirinha_rel = relationship("Carteirinha", back_populates="jobs")
    logs = relationship("Log", back_populates="job_rel")


class BaseGuia(Base):
    __tablename__ = "base_guias"

    id = Column(Integer, primary_key=True, index=True)
    carteirinha_id = Column(Integer, ForeignKey("carteirinhas.id", ondelete="CASCADE"))
    guia = Column(Text)
    data_autorizacao = Column(Date)
    senha = Column(Text)
    validade = Column(Date)
    codigo_procedimento = Column(Text)
    qtde_solicitada = Column(Integer)
    sessoes_autorizadas = Column(Integer)
    valida_prestador = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    carteirinha_rel = relationship("Carteirinha", back_populates="guias")


class Log(Base):
    __tablename__ = "logs"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)
    carteirinha_id = Column(Integer, ForeignKey("carteirinhas.id", ondelete="SET NULL"), nullable=True)
    level = Column(Text, default="INFO")  # INFO, WARN, ERROR
    message = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    job_rel = relationship("Job", back_populates="logs")
    carteirinha_rel = relationship("Carteirinha", back_populates="logs")

class Procedimento(Base):
    __tablename__ = "procedimentos"

    id = Column(Integer, primary_key=True, index=True)
    id_convenio = Column(Integer, index=True)
    nome = Column(Text, nullable=False)
    codigo_procedimento = Column(Text, index=True)
    autorizacao = Column(Text)
    status = Column(Text, default="ativo")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class EvolucaoItem(Base):
    """OP2 ImprimirEvolucao — resultado por linha conciliada (fonte do export de status).

    Espelho do backend/models.py; o scraper persiste aqui (self-persist) durante o
    processamento — a tabela é a fonte da verdade do export.
    """
    __tablename__ = "evolucao_itens"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    lote = Column(Text, index=True)
    id_paciente = Column(Integer, index=True)
    nome_paciente = Column(Text)
    guia = Column(Text)
    data_exec = Column(Date, index=True)
    profissional_id = Column(Integer)          # ID_prof da planilha
    terapia = Column(Text)                     # coluna Terapia (nome)
    profissao_id = Column(Integer, nullable=True)
    hora_inicial = Column(Text)                # "0700"
    status = Column(Text, nullable=False, default="PENDENTE", index=True)
    motivo = Column(Text, nullable=True)
    ids_conciliados = Column(JSONB, nullable=True)
    pdf_path = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    job_rel = relationship("Job")


class EvolucaoClaim(Base):
    """OP2 ImprimirEvolucao — reserva atômica de candidato do portal (UNIQUE fluxo+item)."""
    __tablename__ = "evolucao_claims"

    id = Column(Integer, primary_key=True, index=True)
    fluxo = Column(Text, nullable=False)               # 'aba' | 'evolution'
    portal_item_id = Column(BigInteger, nullable=False)
    job_id = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    job_rel = relationship("Job")
