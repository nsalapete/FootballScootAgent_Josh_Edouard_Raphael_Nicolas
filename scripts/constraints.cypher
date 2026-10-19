// Contraintes et index du graphe. Owner : A. Idempotent (IF NOT EXISTS) : se relance sans risque.
// Les noms des index full-text sont un contrat : player_name_ft et club_name_ft sont utilisés par B et D.

CREATE CONSTRAINT player_id IF NOT EXISTS FOR (p:Player) REQUIRE p.player_id IS UNIQUE;
CREATE CONSTRAINT club_id IF NOT EXISTS FOR (c:Club) REQUIRE c.club_id IS UNIQUE;
CREATE CONSTRAINT competition_id IF NOT EXISTS FOR (c:Competition) REQUIRE c.competition_id IS UNIQUE;
CREATE CONSTRAINT country_code IF NOT EXISTS FOR (c:Country) REQUIRE c.code IS UNIQUE;
CREATE CONSTRAINT transfer_id IF NOT EXISTS FOR (t:Transfer) REQUIRE t.transfer_id IS UNIQUE;
CREATE CONSTRAINT valuation_id IF NOT EXISTS FOR (v:Valuation) REQUIRE v.valuation_id IS UNIQUE;
CREATE CONSTRAINT article_url IF NOT EXISTS FOR (a:Article) REQUIRE a.url IS UNIQUE;
CREATE CONSTRAINT rumour_id IF NOT EXISTS FOR (r:Rumour) REQUIRE r.rumour_id IS UNIQUE;

// Recherche de profils par poste et âge (GET /api/players/search)
CREATE INDEX player_position_dob IF NOT EXISTS FOR (p:Player) ON (p.position, p.date_of_birth);
// Filtre de budget
CREATE INDEX player_value IF NOT EXISTS FOR (p:Player) ON (p.market_value_eur);
// Filières sur une période (GET /api/clubs/{id}/pipeline?since=)
CREATE INDEX transfer_date IF NOT EXISTS FOR (t:Transfer) ON (t.date);
// Rumeurs en attente de confirmation
CREATE INDEX rumour_status IF NOT EXISTS FOR (r:Rumour) ON (r.status);
// Résolution des noms cités par l'utilisateur ou par la presse
CREATE FULLTEXT INDEX player_name_ft IF NOT EXISTS FOR (p:Player) ON EACH [p.name];
CREATE FULLTEXT INDEX club_name_ft IF NOT EXISTS FOR (c:Club) ON EACH [c.name];
