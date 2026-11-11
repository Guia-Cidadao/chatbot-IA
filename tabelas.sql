CREATE TABLE servicos (
id BIGINT NOT NULL AUTO_INCREMENT,
nome VARCHAR(255) NOT NULL,
descricao VARCHAR(255) NOT NULL,

CONSTRAINT pk_servicos PRIMARY KEY (id),
CONSTRAINT uk_servicos_nome UNIQUE (nome)
);

CREATE TABLE servicos_documentos (
servico_id BIGINT NOT NULL,
ordem INT NOT NULL,
documento VARCHAR(255) NOT NULL,

CONSTRAINT pk_servicos_documentos
    PRIMARY KEY (servico_id, ordem),

CONSTRAINT fk_servicos_documentos_servicos
    FOREIGN KEY (servico_id)
        REFERENCES servicos(id)
        ON DELETE CASCADE
);