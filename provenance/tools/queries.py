from __future__ import annotations


QUERY_REGISTRY: dict[str, str] = {
    "coordination.task.write@1": """
        MERGE (task:Task {id: $task_id})
        ON CREATE SET task.created_ts = $created_ts
        SET task.kind = $kind, task.status = $status, task.updated_ts = $updated_ts
        MERGE (agent:Agent {name: $agent_name})
        MERGE (task)-[:CLAIMED_BY]->(agent)
        RETURN task.id
    """,
    "coordination.schema.indexes@1": """
        CALL db.indexes() YIELD label, properties
        RETURN label, properties
    """,
    "coordination.episode.write@1": """
        MATCH (task:Task {id: $task_id})
        MERGE (episode:Episode {id: $episode_id})
        SET episode.agent_name = $agent_name, episode.subject = $subject,
            episode.question = $question, episode.answer_summary = $answer_summary,
            episode.ts = $ts, episode.evidence_node_ids = $node_ids,
            episode.evidence_edge_ids = $edge_ids, episode.facts_json = $facts_json
        MERGE (task)-[:PRODUCED]->(episode)
        RETURN episode.id
    """,
    "coordination.task.depends_on@1": """
        MATCH (task:Task {id: $task_id})
        MATCH (dependency:Task {id: $depends_on_task_id})
        WHERE task.id <> dependency.id
        MERGE (task)-[:DEPENDS_ON]->(dependency)
        RETURN task.id, dependency.id
    """,
    "coordination.task.exists@1": """
        MATCH (task:Task {id: $task_id})
        RETURN task.id
    """,
    "coordination.task.complete@1": """
        MATCH (task:Task {id: $task_id})
        SET task.status = 'completed', task.updated_ts = $updated_ts
        RETURN task.id
    """,
    "coordination.episode.used_fact@1": """
        MATCH (episode:Episode {id: $episode_id})
        MATCH (evidence) WHERE id(evidence) IN $node_ids
        MERGE (episode)-[:USED_FACT {node_id: id(evidence)}]->(evidence)
        RETURN id(evidence)
    """,
    "coordination.episode.latest_subject@1": """
        MATCH (episode:Episode {agent_name: $agent_name, subject: $subject})
        RETURN episode.id, episode.answer_summary, episode.ts
        ORDER BY episode.ts DESC
        LIMIT 1
    """,
    "coordination.episode.by_id@1": """
        MATCH (episode:Episode {id: $episode_id})
        RETURN episode.facts_json, episode.evidence_node_ids,
               episode.evidence_edge_ids
    """,
    "coordination.episode.used_fact_count@1": """
        MATCH (episode:Episode {id: $episode_id})-[:USED_FACT]->(evidence)
        RETURN count(evidence)
    """,
    "coordination.graph.tasks@1": """
        MATCH (task:Task)
        OPTIONAL MATCH (task)-[:CLAIMED_BY]->(agent:Agent)
        OPTIONAL MATCH (task)-[:PRODUCED]->(episode:Episode)
        OPTIONAL MATCH (task)-[:DEPENDS_ON]->(dependency:Task)
        RETURN task.id, task.kind, task.status, agent.name, episode.id,
               episode.answer_summary, dependency.id,
               episode.evidence_node_ids, episode.evidence_edge_ids
        ORDER BY task.created_ts, task.id
    """,
    "coordination.graph.task_count@1": """
        MATCH (task:Task)
        RETURN count(task)
    """,
    "coordination.tenant.modules@1": """
        MATCH (module:Module)
        WHERE module.key IN $module_keys
        RETURN module.key, module.kind, module.name
        ORDER BY module.key
    """,
    "coordination.tenant.dependencies@1": """
        MATCH (source:Module)-[edge:DEPENDS_ON]->(target:Module)
        WHERE source.key IN $module_keys AND target.key IN $module_keys
        RETURN source.key, source.kind, source.name, target.key, target.kind,
               target.name, edge.kind
        ORDER BY source.key, target.key, edge.kind
    """,
    "coordination.tenant.activity@1": """
        MATCH (person:Person)-[activity:AUTHORED]->(pr:PR)-[:TOUCHES]->(module:Module)
        WHERE module.key IN $module_keys
        RETURN person.login AS login, person.is_bot AS is_bot,
               pr.number AS pr_number, pr.title AS title, pr.merged_ts AS merged_ts,
               pr.additions AS additions, pr.deletions AS deletions, pr.body AS body,
               module.key AS module_key, 'AUTHORED' AS activity_type,
               activity.ts AS activity_ts, activity.role AS role, activity.state AS state
        UNION ALL
        MATCH (person:Person)-[activity:REVIEWED]->(pr:PR)-[:TOUCHES]->(module:Module)
        WHERE module.key IN $module_keys
        RETURN person.login AS login, person.is_bot AS is_bot,
               pr.number AS pr_number, pr.title AS title, pr.merged_ts AS merged_ts,
               pr.additions AS additions, pr.deletions AS deletions, pr.body AS body,
               module.key AS module_key, 'REVIEWED' AS activity_type,
               activity.ts AS activity_ts, activity.role AS role, activity.state AS state
    """,
    "coordination.tenant.person_owners@1": """
        MATCH (owner:Person)-[:DECLARED_OWNER]->(module:Module)
        WHERE module.key IN $module_keys
        RETURN owner.login, owner.is_bot, module.key
    """,
    "coordination.tenant.team_owners@1": """
        MATCH (owner:Team)-[:DECLARED_OWNER]->(module:Module)
        WHERE module.key IN $module_keys
        RETURN owner.handle, module.key
    """,
    "coordination.tenant.copy.modules@1": """
        UNWIND $rows AS row
        MERGE (module:Module {key: row.key})
        SET module.kind = row.kind, module.name = row.name
        RETURN count(module)
    """,
    "coordination.tenant.copy.people@1": """
        UNWIND $rows AS row
        MERGE (person:Person {login: row.login})
        SET person.is_bot = row.is_bot
        RETURN count(person)
    """,
    "coordination.tenant.copy.teams@1": """
        UNWIND $rows AS row
        MERGE (team:Team {handle: row.handle})
        RETURN count(team)
    """,
    "coordination.tenant.copy.prs@1": """
        UNWIND $rows AS row
        MERGE (pr:PR {number: row.number})
        SET pr.title = row.title, pr.merged_ts = row.merged_ts,
            pr.additions = row.additions, pr.deletions = row.deletions,
            pr.body = row.body
        RETURN count(pr)
    """,
    "coordination.tenant.copy.touches@1": """
        UNWIND $rows AS row
        MATCH (pr:PR {number: row.pr_number})
        MATCH (module:Module {key: row.module_key})
        MERGE (pr)-[:TOUCHES]->(module)
        RETURN count(pr)
    """,
    "coordination.tenant.copy.authored@1": """
        UNWIND $rows AS row
        MATCH (person:Person {login: row.login})
        MATCH (pr:PR {number: row.pr_number})
        MERGE (person)-[edge:AUTHORED]->(pr)
        SET edge.ts = row.ts, edge.role = row.role, edge.state = row.state
        RETURN count(person)
    """,
    "coordination.tenant.copy.person_owners@1": """
        UNWIND $rows AS row
        MATCH (owner:Person {login: row.login})
        MATCH (module:Module {key: row.module_key})
        MERGE (owner)-[:DECLARED_OWNER]->(module)
        RETURN count(owner)
    """,
    "coordination.tenant.copy.team_owners@1": """
        UNWIND $rows AS row
        MATCH (owner:Team {handle: row.handle})
        MATCH (module:Module {key: row.module_key})
        MERGE (owner)-[:DECLARED_OWNER]->(module)
        RETURN count(owner)
    """,
    "coordination.tenant.copy.reviewed@1": """
        UNWIND $rows AS row
        MATCH (person:Person {login: row.login})
        MATCH (pr:PR {number: row.pr_number})
        MERGE (person)-[edge:REVIEWED]->(pr)
        SET edge.ts = row.ts, edge.role = row.role, edge.state = row.state
        RETURN count(person)
    """,
    "coordination.tenant.copy.dependencies@1": """
        UNWIND $rows AS row
        MATCH (source:Module {key: row.source_key})
        MATCH (target:Module {key: row.target_key})
        MERGE (source)-[:DEPENDS_ON {kind: row.kind}]->(target)
        RETURN count(source)
    """,
    "coordination.demo.sample@1": """
        MATCH (person:Person)-[:AUTHORED]->(pr:PR)-[:TOUCHES]->(module:Module)
        WHERE person.is_bot = false AND module.kind = 'integration'
        RETURN pr.number, module.key, person.login
        ORDER BY pr.merged_ts DESC
        LIMIT 1
    """,
    "coordination.tenant.index.module@1": """
        CREATE INDEX FOR (module:Module) ON (module.key)
    """,
    "coordination.tenant.index.person@1": """
        CREATE INDEX FOR (person:Person) ON (person.login)
    """,
    "coordination.tenant.index.team@1": """
        CREATE INDEX FOR (team:Team) ON (team.handle)
    """,
    "coordination.tenant.index.pr@1": """
        CREATE INDEX FOR (pr:PR) ON (pr.number)
    """,
    "coordination.tenant.index.task@1": """
        CREATE INDEX FOR (task:Task) ON (task.id)
    """,
    "coordination.tenant.index.episode@1": """
        CREATE INDEX FOR (episode:Episode) ON (episode.id)
    """,
    "meta.graph_bounds@1": """
        MATCH (pr:PR)
        RETURN min(pr.merged_ts), max(pr.merged_ts)
    """,
    "meta.as_of@1": """
        RETURN $as_of_ts
    """,
    "meta.tool_config@1": """
        RETURN $activity_window_days, $expertise_window_days,
             $expertise_half_life_days, $recent_half_life_days, $g3_lambda,
             $bus_factor_share_threshold,
               $bus_factor_min_prs, $uncovered_person_activity_prs,
                             $ownership_drift_ownerless_weight, $ownership_drift_stale_weight,
                             $ownership_drift_outside_top5_weight,
               $default_top_n, $default_max_depth,
               $risk_bus_factor_weight, $risk_ownership_drift_weight,
               $risk_criticality_weight, $risk_blast_radius_weight
    """,
    "modules.list@1": """
        MATCH (module:Module)
        RETURN module.key, module.kind, module.name, id(module)
        ORDER BY module.key
    """,
    "module.metadata@1": """
        MATCH (module:Module {key: $module_key})
        RETURN module.key, module.kind, module.name, id(module)
    """,
    "module.pr_count@1": """
        MATCH (pr:PR)-[touch:TOUCHES]->(module:Module {key: $module_key})
        WHERE pr.merged_ts >= $activity_start_ts AND pr.merged_ts <= $as_of_ts
        RETURN count(DISTINCT pr.number), collect(DISTINCT id(pr)),
               collect(DISTINCT id(touch)), id(module)
    """,
        "module.pr_counts@1": """
                MATCH (pr:PR)-[touch:TOUCHES]->(module:Module)
                WHERE module.key IN $module_keys
                    AND pr.merged_ts >= $activity_start_ts AND pr.merged_ts <= $as_of_ts
                RETURN module.key, count(DISTINCT pr.number),
                             collect(DISTINCT id(pr)), collect(DISTINCT id(touch)), id(module)
                ORDER BY module.key
        """,
            "person.by_login@1": """
                MATCH (person:Person {login: $person_login})
                RETURN person.login, person.is_bot, id(person)
            """,
    "module.owners@1": """
        MATCH (owner)-[owner_edge:DECLARED_OWNER]->(module:Module {key: $module_key})
         RETURN module.key, owner.login, owner.handle, owner.is_bot,
             id(module), id(owner), id(owner_edge)
        ORDER BY owner.login, owner.handle
    """,
    "activity.rows@1": """
        MATCH (person:Person)-[authored:AUTHORED]->(pr:PR)-[touch:TOUCHES]->(module:Module)
        WHERE pr.merged_ts >= $activity_start_ts AND pr.merged_ts <= $as_of_ts
          AND authored.ts <= $as_of_ts AND person.is_bot = false
          AND ($module_key IS NULL OR module.key = $module_key)
          AND ($person_login IS NULL OR person.login = $person_login)
          AND ($module_keys IS NULL OR module.key IN $module_keys)
         RETURN module.key AS module_key, module.kind AS module_kind,
             person.login AS login, 'author' AS role, pr.number AS pr_number,
             id(module) AS module_id, id(person) AS person_id, id(pr) AS pr_id,
             id(authored) AS event_id, id(touch) AS touch_id, $top_n AS top_n
        UNION ALL
        MATCH (person:Person)-[review:REVIEWED]->(pr:PR)-[touch:TOUCHES]->(module:Module)
        WHERE pr.merged_ts >= $activity_start_ts AND pr.merged_ts <= $as_of_ts
          AND review.ts <= $as_of_ts AND person.is_bot = false
          AND ($module_key IS NULL OR module.key = $module_key)
          AND ($person_login IS NULL OR person.login = $person_login)
          AND ($module_keys IS NULL OR module.key IN $module_keys)
         RETURN module.key AS module_key, module.kind AS module_kind,
             person.login AS login, 'review' AS role, pr.number AS pr_number,
             id(module) AS module_id, id(person) AS person_id, id(pr) AS pr_id,
             id(review) AS event_id, id(touch) AS touch_id, $top_n AS top_n
    """,
    "activity.owners_and_people@1": """
        MATCH (owner)-[declared:DECLARED_OWNER]->(module:Module {key: $module_key})
         RETURN 'owner' AS row_type, module.key AS module_key,
             owner.login AS login, owner.handle AS handle, owner.is_bot AS is_bot,
             null AS role, id(module) AS module_id, id(owner) AS person_id,
             null AS pr_id, id(declared) AS event_id, null AS touch_id,
             $top_n AS top_n
        UNION ALL
        MATCH (person:Person)-[authored:AUTHORED]->(pr:PR)-[touch:TOUCHES]->(module:Module {key: $module_key})
        WHERE pr.merged_ts >= $activity_start_ts AND pr.merged_ts <= $as_of_ts
          AND authored.ts <= $as_of_ts AND person.is_bot = false
         RETURN 'activity' AS row_type, module.key AS module_key,
             person.login AS login, null AS handle, person.is_bot AS is_bot,
             'author' AS role, id(module) AS module_id, id(person) AS person_id,
             id(pr) AS pr_id, id(authored) AS event_id, id(touch) AS touch_id,
             $top_n AS top_n
        UNION ALL
        MATCH (person:Person)-[review:REVIEWED]->(pr:PR)-[touch:TOUCHES]->(module:Module {key: $module_key})
        WHERE pr.merged_ts >= $activity_start_ts AND pr.merged_ts <= $as_of_ts
          AND review.ts <= $as_of_ts AND person.is_bot = false
         RETURN 'activity' AS row_type, module.key AS module_key,
             person.login AS login, null AS handle, person.is_bot AS is_bot,
             'review' AS role, id(module) AS module_id, id(person) AS person_id,
             id(pr) AS pr_id, id(review) AS event_id, id(touch) AS touch_id,
             $top_n AS top_n
    """,
    "module.decisions@1": """
        MATCH (pr:PR)-[touch:TOUCHES]->(module:Module {key: $module_key})
        WHERE pr.merged_ts <= $as_of_ts
        MATCH (pr)-[reference:REFERENCES]->(decision:Decision)
         RETURN pr.number AS pr_number, decision.id AS decision_id,
             decision.title AS title, decision.status AS status,
             decision.source AS source, 'REFERENCES' AS relation,
             id(pr) AS pr_id, id(module) AS module_id,
             id(decision) AS decision_node_id, id(touch) AS touch_id,
             id(reference) AS decision_edge_id
        UNION ALL
        MATCH (pr:PR)-[touch:TOUCHES]->(module:Module {key: $module_key})
        WHERE pr.merged_ts <= $as_of_ts
        MATCH (pr)-[decided:DECIDED_AS]->(decision:Decision)
         RETURN pr.number AS pr_number, decision.id AS decision_id,
             decision.title AS title, decision.status AS status,
             decision.source AS source, 'DECIDED_AS' AS relation,
             id(pr) AS pr_id, id(module) AS module_id,
             id(decision) AS decision_node_id, id(touch) AS touch_id,
             id(decided) AS decision_edge_id
    """,
        "ownership.drift.inputs@1": """
         MATCH (owner)-[declared:DECLARED_OWNER]->(module:Module {key: $module_key})
         RETURN 'owner' AS row_type, owner.login AS login,
             owner.handle AS handle, owner.is_bot AS is_bot,
             null AS role, id(module) AS module_id, id(owner) AS person_id,
             null AS pr_id, id(declared) AS event_id, null AS touch_id
         UNION ALL
         MATCH (person:Person)-[authored:AUTHORED]->(pr:PR)-[touch:TOUCHES]->(module:Module {key: $module_key})
         WHERE pr.merged_ts >= $activity_start_ts AND pr.merged_ts <= $as_of_ts
           AND authored.ts <= $as_of_ts AND person.is_bot = false
         RETURN 'activity' AS row_type, person.login AS login,
             null AS handle, person.is_bot AS is_bot, 'author' AS role,
             id(module) AS module_id, id(person) AS person_id, id(pr) AS pr_id,
             id(authored) AS event_id, id(touch) AS touch_id
         UNION ALL
         MATCH (person:Person)-[review:REVIEWED]->(pr:PR)-[touch:TOUCHES]->(module:Module {key: $module_key})
         WHERE pr.merged_ts >= $activity_start_ts AND pr.merged_ts <= $as_of_ts
           AND review.ts <= $as_of_ts AND person.is_bot = false
         RETURN 'activity' AS row_type, person.login AS login,
             null AS handle, person.is_bot AS is_bot, 'review' AS role,
             id(module) AS module_id, id(person) AS person_id, id(pr) AS pr_id,
             id(review) AS event_id, id(touch) AS touch_id
        """,
        "ownership.drift.inputs.all@1": """
         MATCH (owner)-[declared:DECLARED_OWNER]->(module:Module)
         WHERE module.key IN $module_keys
         RETURN module.key AS module_key, 'owner' AS row_type,
             owner.login AS login, owner.handle AS handle,
             owner.is_bot AS is_bot, null AS role, id(module) AS module_id,
             id(owner) AS person_id, null AS pr_id, id(declared) AS event_id,
             null AS touch_id
         UNION ALL
         MATCH (person:Person)-[authored:AUTHORED]->(pr:PR)-[touch:TOUCHES]->(module:Module)
         WHERE module.key IN $module_keys
           AND pr.merged_ts >= $activity_start_ts AND pr.merged_ts <= $as_of_ts
           AND authored.ts <= $as_of_ts AND person.is_bot = false
         RETURN module.key AS module_key, 'activity' AS row_type,
             person.login AS login, null AS handle, person.is_bot AS is_bot,
             'author' AS role, id(module) AS module_id, id(person) AS person_id,
             id(pr) AS pr_id, id(authored) AS event_id, id(touch) AS touch_id
         UNION ALL
         MATCH (person:Person)-[review:REVIEWED]->(pr:PR)-[touch:TOUCHES]->(module:Module)
         WHERE module.key IN $module_keys
           AND pr.merged_ts >= $activity_start_ts AND pr.merged_ts <= $as_of_ts
           AND review.ts <= $as_of_ts AND person.is_bot = false
         RETURN module.key AS module_key, 'activity' AS row_type,
             person.login AS login, null AS handle, person.is_bot AS is_bot,
             'review' AS role, id(module) AS module_id, id(person) AS person_id,
             id(pr) AS pr_id, id(review) AS event_id, id(touch) AS touch_id
        """,
        "bus_factor.inputs@1": """
         MATCH (pr:PR)-[touch:TOUCHES]->(module:Module)
         WHERE pr.merged_ts >= $activity_start_ts AND pr.merged_ts <= $as_of_ts
               AND ($module_key IS NULL OR module.key = $module_key)
             RETURN module.key AS module_key, module.kind AS module_kind,
                 'pr' AS row_type, null AS login, pr.number AS pr_number,
                 id(module) AS module_id, null AS person_id, id(pr) AS pr_id,
                 null AS event_id, id(touch) AS touch_id, $top_n AS top_n,
                 $bus_factor_share_threshold AS share_threshold,
                 $bus_factor_min_prs AS minimum_prs
         UNION ALL
         MATCH (person:Person)-[review:REVIEWED]->(pr:PR)-[touch:TOUCHES]->(module:Module)
         WHERE pr.merged_ts >= $activity_start_ts AND pr.merged_ts <= $as_of_ts
               AND review.ts <= $as_of_ts AND person.is_bot = false
               AND ($module_key IS NULL OR module.key = $module_key)
         RETURN module.key AS module_key, module.kind AS module_kind,
             'review' AS row_type, person.login AS login, pr.number AS pr_number,
             id(module) AS module_id, id(person) AS person_id, id(pr) AS pr_id,
             id(review) AS event_id, id(touch) AS touch_id, $top_n AS top_n,
             $bus_factor_share_threshold AS share_threshold,
             $bus_factor_min_prs AS minimum_prs
        """,
            "owners.all@1": """
             MATCH (owner)-[owner_edge:DECLARED_OWNER]->(module:Module)
             RETURN module.key, owner.login, owner.handle, owner.is_bot,
                 id(module), id(owner), id(owner_edge)
             ORDER BY module.key, owner.login, owner.handle
            """,
    "decision.by_pr@1": """
        MATCH (pr:PR {number: $pr_number})
        WHERE pr.merged_ts <= $as_of_ts
        MATCH (pr)-[reference:REFERENCES]->(decision:Decision)
         RETURN pr.number AS pr_number, decision.id AS decision_id,
             decision.title AS title, decision.status AS status,
             decision.source AS source, 'REFERENCES' AS relation,
             id(pr) AS pr_id, id(decision) AS decision_node_id,
             id(reference) AS decision_edge_id
        UNION ALL
        MATCH (pr:PR {number: $pr_number})
        WHERE pr.merged_ts <= $as_of_ts
        MATCH (pr)-[decided:DECIDED_AS]->(decision:Decision)
         RETURN pr.number AS pr_number, decision.id AS decision_id,
             decision.title AS title, decision.status AS status,
             decision.source AS source, 'DECIDED_AS' AS relation,
             id(pr) AS pr_id, id(decision) AS decision_node_id,
             id(decided) AS decision_edge_id
    """,
    "decision.supersedes_chain@1": """
         MATCH (start:Decision)
         WHERE start.id IN $decision_ids
         OPTIONAL MATCH path=(start)-[supersedes:SUPERSEDES*1..]->(older:Decision)
         RETURN start.id, collect(DISTINCT {
                 decision_id: older.id,
                 title: older.title,
                 status: older.status,
                 source: older.source
             }), id(start),
             collect(DISTINCT id(older)),
             collect(DISTINCT [edge IN relationships(path) | id(edge)])
         ORDER BY start.id
    """,
    "blast.paths.depth1@1": """
        MATCH (dependent:Module)-[edge:DEPENDS_ON]->(target:Module {key: $module_key})
        RETURN dependent.key, dependent.kind, 1, id(dependent), id(edge), id(target)
        ORDER BY dependent.key
    """,
    "blast.paths.depth2@1": """
        MATCH path=(dependent:Module)-[edges:DEPENDS_ON*1..2]->(target:Module {key: $module_key})
        WHERE length(path) <= $max_depth
        RETURN dependent.key, dependent.kind, length(path), id(dependent),
               [edge IN relationships(path) | id(edge)], id(target)
        ORDER BY dependent.key, length(path)
    """,
    "blast.edges@1": """
        MATCH (source:Module)-[edge:DEPENDS_ON]->(target:Module)
        RETURN source.key, target.key, source.kind, target.kind,
               id(source), id(edge), id(target)
        ORDER BY target.key, source.key
    """,
    "criticality.page_rank@1": """
        CALL algo.pageRank($module_label, $relationship_type)
        YIELD node, score
        WHERE node.kind IN $module_kinds
        RETURN node.key, node.kind, score, id(node)
        ORDER BY score DESC, node.key
    """,
    "criticality.in_degree@1": """
        MATCH (source:Module)-[edge:DEPENDS_ON]->(target:Module)
        WHERE target.kind IN $module_kinds
        RETURN target.key, count(edge), id(target), collect(DISTINCT id(edge))
        ORDER BY target.key
    """,
    "reviewer.g3.target_pr@1": """
        MATCH (author:Person)-[authored:AUTHORED]->(pr:PR)
        WHERE pr.number = $pr_number AND pr.merged_ts <= $as_of_ts
          AND authored.ts <= $as_of_ts
        MATCH (pr)-[touch:TOUCHES]->(module:Module)
        RETURN pr.number, pr.merged_ts, author.login, author.is_bot,
               module.key, module.kind, id(pr), id(author), id(authored),
               id(module), id(touch)
        ORDER BY module.key
    """,
    "reviewer.g3.target_pr@2": """
        MATCH (author:Person)-[authored:AUTHORED]->(pr:PR {number: $pr_number})
        MATCH (pr)-[touch:TOUCHES]->(module:Module)
        RETURN pr.number, pr.merged_ts, author.login, author.is_bot,
               module.key, module.kind, id(pr), id(author), id(authored),
               id(module), id(touch)
        ORDER BY module.key
    """,
    "reviewer.g3.global@1": """
        MATCH (person:Person)-[review:REVIEWED]->(pr:PR)
                WHERE pr.merged_ts >= $expertise_start_ts AND pr.merged_ts <= $as_of_ts
                    AND review.ts <= $as_of_ts
        RETURN person.login, person.is_bot, count(review),
               sum(exp(-0.6931471805599453 * ($as_of_ts - review.ts) / $b3_half_life_seconds)),
               collect(DISTINCT pr.number), id(person), collect(DISTINCT id(review))
        ORDER BY person.login
    """,
    "reviewer.g3.expertise@1": """
        MATCH (history:PR)-[all_touch:TOUCHES]->(:Module)
                WHERE history.merged_ts >= $expertise_start_ts
                    AND history.merged_ts <= $as_of_ts
        WITH history, count(all_touch) AS touched_module_count
        MATCH (history)-[target_touch:TOUCHES]->(module:Module)
        WHERE module.key IN $module_keys
        MATCH (person:Person)-[review:REVIEWED]->(history)
        WHERE review.ts <= $as_of_ts AND person.is_bot = false
        WITH DISTINCT person, module, history, target_touch, touched_module_count, review
        RETURN 'review' AS role, person.login AS login, module.key AS module_key,
               sum(exp(-0.6931471805599453 * ($as_of_ts - history.merged_ts) / $g1_half_life_seconds) / touched_module_count),
               count(target_touch) AS touch_count,
               collect(DISTINCT history.number) AS pr_numbers,
               id(person) AS person_id, id(module) AS module_id,
               collect(DISTINCT id(review)) AS evidence_edge_ids
        UNION ALL
        MATCH (history:PR)-[all_touch:TOUCHES]->(:Module)
        WHERE history.merged_ts <= $as_of_ts
        WITH history, count(all_touch) AS touched_module_count
        MATCH (history)-[target_touch:TOUCHES]->(module:Module)
        WHERE module.key IN $module_keys
        MATCH (person:Person)-[authored:AUTHORED]->(history)
        WHERE authored.ts <= $as_of_ts AND person.is_bot = false
        WITH DISTINCT person, module, history, target_touch, touched_module_count, authored
        RETURN 'author' AS role, person.login AS login, module.key AS module_key,
               sum(exp(-0.6931471805599453 * ($as_of_ts - history.merged_ts) / $g1_half_life_seconds) / touched_module_count),
               count(target_touch) AS touch_count,
               collect(DISTINCT history.number) AS pr_numbers,
               id(person) AS person_id, id(module) AS module_id,
               collect(DISTINCT id(authored)) AS evidence_edge_ids
    """,
    "maintainer.dependents.depth1@1": """
        UNWIND $module_keys AS root_key
        MATCH (dependent:Module)-[edge:DEPENDS_ON]->(target:Module {key: root_key})
        RETURN root_key, dependent.key, 1, id(dependent), id(edge), id(target)
        ORDER BY root_key, dependent.key
    """,
    "maintainer.dependents.depth2@1": """
        MATCH path=(dependent:Module)-[edges:DEPENDS_ON*1..2]->(target:Module)
        WHERE target.key IN $module_keys AND length(path) <= $max_depth
        RETURN target.key, dependent.key, length(path), id(dependent),
               [edge IN relationships(path) | id(edge)], id(target)
        ORDER BY target.key, dependent.key, length(path)
    """,
    "api.subgraph.nodes_by_id@1": """
        MATCH (node)
        WHERE id(node) IN $ids
        RETURN id(node), labels(node), node.login, node.key, node.name,
               node.number, node.title, node.status, node.kind, node.ts
    """,
    "api.subgraph.edges_by_id@1": """
        MATCH (source)-[edge]->(target)
        WHERE id(edge) IN $ids
        RETURN id(edge), type(edge), id(source), id(target),
               edge.kind, edge.status, edge.ts
    """,
    "api.health.node_counts@1": """
        MATCH (node)
        RETURN labels(node), count(node)
    """,
    "api.health.edge_counts@1": """
        MATCH ()-[edge]->()
        RETURN type(edge), count(edge)
    """,
    "api.search.people@1": """
        MATCH (person:Person)
        WHERE toLower(person.login) CONTAINS toLower($q)
        RETURN person.login, id(person)
        ORDER BY person.login
        LIMIT 10
    """,
    "api.neighborhood.dependents@1": """
        MATCH (dependent:Module)-[edge:DEPENDS_ON]->(target:Module {key: $module_key})
        RETURN dependent.key, dependent.kind, id(dependent),
               collect(DISTINCT id(edge)), id(target)
        ORDER BY dependent.key
        LIMIT 31
    """,
    "maintainer.top_reviewers@1": """
        MATCH (person:Person)-[review:REVIEWED]->(pr:PR)
        WHERE pr.merged_ts >= $activity_start_ts AND pr.merged_ts <= $as_of_ts
          AND review.ts <= $as_of_ts AND person.is_bot = false
         RETURN person.login, count(DISTINCT pr.number), id(person),
             collect(DISTINCT pr.number), collect(DISTINCT id(review))
        ORDER BY count(DISTINCT pr.number) DESC, person.login
    """,
    "reviewer.g3.bundle@1": """
        MATCH (person:Person)-[review:REVIEWED]->(pr:PR)
        WHERE pr.merged_ts >= $expertise_start_ts AND pr.merged_ts <= $as_of_ts
          AND review.ts <= $as_of_ts
        RETURN 'global' AS row_type, person.login AS login, '' AS module_key,
               count(review) AS event_count,
               sum(exp(-0.6931471805599453 * ($as_of_ts - review.ts) / $b3_half_life_seconds)) AS recent_score,
               0.0 AS module_score, collect(DISTINCT pr.number) AS pr_numbers,
                             collect(DISTINCT id(review)) AS evidence_edge_ids,
               id(person) AS person_id, -1 AS module_id
        UNION ALL
        MATCH (history:PR)-[all_touch:TOUCHES]->(:Module)
        WHERE history.merged_ts >= $expertise_start_ts AND history.merged_ts <= $as_of_ts
        WITH history, count(all_touch) AS touched_module_count
        MATCH (history)-[target_touch:TOUCHES]->(module:Module)
        WHERE module.key IN $module_keys
        MATCH (person:Person)-[review:REVIEWED]->(history)
        WHERE review.ts <= $as_of_ts AND person.is_bot = false
           WITH person, module, history, target_touch, touched_module_count,
               collect(DISTINCT id(review)) AS review_ids
        RETURN 'module_review' AS row_type, person.login AS login, module.key AS module_key,
               count(target_touch) AS event_count, 0.0 AS recent_score,
               sum(exp(-0.6931471805599453 * ($as_of_ts - history.merged_ts) / $g1_half_life_seconds) / touched_module_count) AS module_score,
               collect(DISTINCT history.number) AS pr_numbers,
               collect(DISTINCT review_ids) AS evidence_edge_ids,
               id(person) AS person_id, id(module) AS module_id
        UNION ALL
        MATCH (history:PR)-[all_touch:TOUCHES]->(:Module)
        WHERE history.merged_ts >= $expertise_start_ts AND history.merged_ts <= $as_of_ts
        WITH history, count(all_touch) AS touched_module_count
        MATCH (history)-[target_touch:TOUCHES]->(module:Module)
        WHERE module.key IN $module_keys
        MATCH (person:Person)-[authored:AUTHORED]->(history)
        WHERE authored.ts <= $as_of_ts AND person.is_bot = false
        WITH DISTINCT person, module, history, target_touch, touched_module_count, authored
        RETURN 'module_author' AS row_type, person.login AS login, module.key AS module_key,
               count(target_touch) AS event_count, 0.0 AS recent_score,
               sum(exp(-0.6931471805599453 * ($as_of_ts - history.merged_ts) / $g1_half_life_seconds) / touched_module_count) AS module_score,
               collect(DISTINCT history.number) AS pr_numbers,
               collect(DISTINCT id(authored)) AS evidence_edge_ids,
                             id(person) AS person_id, id(module) AS module_id
    """,
        "reviewer.support_edges@1": """
                MATCH (p:Person)-[r:REVIEWED]->(pr:PR)
                WHERE p.login IN $candidate_logins
                    AND pr.merged_ts >= $expertise_start_ts AND pr.merged_ts <= $as_of_ts
                    AND r.ts <= $as_of_ts
                 RETURN p.login AS login, '' AS module_key, pr.number AS pr_number,
                     id(p) AS person_id, id(pr) AS pr_id, null AS module_id,
                     id(r) AS review_edge_id, null AS touch_id,
                     pr.merged_ts AS event_ts
                UNION ALL
                MATCH (p:Person)-[r:REVIEWED]->(pr:PR)-[t:TOUCHES]->(m:Module)
                WHERE p.login IN $candidate_logins AND m.key IN $module_keys
                    AND pr.merged_ts >= $expertise_start_ts AND pr.merged_ts <= $as_of_ts
                    AND r.ts <= $as_of_ts
                 RETURN p.login AS login, m.key AS module_key, pr.number AS pr_number,
                     id(p) AS person_id, id(pr) AS pr_id, id(m) AS module_id,
                     id(r) AS review_edge_id, id(t) AS touch_id,
                             pr.merged_ts AS event_ts
                 ORDER BY event_ts DESC, login
                LIMIT 120
        """,
}

QUERY_EVIDENCE_COLUMNS: dict[str, dict[str, tuple[int, ...]]] = {
    "meta.as_of@1": {},
    "meta.tool_config@1": {},
    "modules.list@1": {"nodes": (3,)},
    "module.metadata@1": {"nodes": (3,)},
    "module.pr_count@1": {"nodes": (3,), "edges": (2,)},
    "module.pr_counts@1": {"nodes": (2, 4), "edges": (3,)},
    "person.by_login@1": {"nodes": (2,)},
    "module.owners@1": {"nodes": (4, 5), "edges": (6,)},
    "activity.rows@1": {"nodes": (5, 6, 7), "edges": (8, 9)},
    "activity.owners_and_people@1": {"nodes": (6, 7, 8), "edges": (9, 10)},
    "owners.all@1": {"nodes": (4, 5), "edges": (6,)},
    "ownership.drift.inputs@1": {"nodes": (6, 7, 8), "edges": (9, 10)},
    "bus_factor.inputs@1": {"nodes": (5, 6, 7), "edges": (8, 9)},
    "module.decisions@1": {"nodes": (6, 7, 8), "edges": (9, 10)},
    "reviewer.g3.target_pr@1": {"nodes": (6, 7, 9), "edges": (8, 10)},
    "reviewer.g3.target_pr@2": {"nodes": (6, 7, 9), "edges": (8, 10)},
    "reviewer.g3.global@1": {"nodes": (5,), "edges": (6,)},
    "reviewer.g3.expertise@1": {"nodes": (6, 7), "edges": (8,)},
    "maintainer.top_reviewers@1": {"nodes": (2,), "edges": (4,)},
    "reviewer.g3.bundle@1": {"nodes": (8, 9)},
    "reviewer.support_edges@1": {"nodes": (3, 4, 5), "edges": (6, 7)},
    "blast.edges@1": {"nodes": (4, 6), "edges": (5,)},
    "ownership.drift.inputs@1": {"nodes": (5, 6, 7), "edges": (8, 9)},
    "ownership.drift.inputs.all@1": {"nodes": (6, 7, 8), "edges": (9, 10)},
    "bus_factor.inputs@1": {"nodes": (5, 6, 7), "edges": (8, 9)},
    "decision.by_pr@1": {"nodes": (6, 7), "edges": (8,)},
    "decision.supersedes_chain@1": {"nodes": (2, 3), "edges": (4,)},
    "blast.paths.depth1@1": {"nodes": (3, 5), "edges": (4,)},
    "blast.paths.depth2@1": {"nodes": (3, 5), "edges": (4,)},
    "criticality.page_rank@1": {"nodes": (3,)},
    "criticality.in_degree@1": {"nodes": (2,), "edges": (3,)},
    "maintainer.dependents.depth1@1": {"nodes": (3, 5), "edges": (4,)},
    "maintainer.dependents.depth2@1": {"nodes": (3, 5), "edges": (4,)},
    "api.subgraph.nodes_by_id@1": {"nodes": (0,)},
    "api.subgraph.edges_by_id@1": {"nodes": (2, 3), "edges": (0,)},
    "api.health.node_counts@1": {},
    "api.health.edge_counts@1": {},
    "api.search.people@1": {"nodes": (1,)},
    "api.neighborhood.dependents@1": {"nodes": (2, 4), "edges": (3,)},
}