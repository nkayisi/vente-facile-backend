"""
Services pour l'importation et l'exportation de produits via Excel.
"""
import hashlib
import io
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Tuple, Optional, Any

from django.db import transaction
from django.utils.text import slugify
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side, Protection
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from .models import Product, Category, Brand, Unit


# Signature secrète pour valider l'authenticité du template
TEMPLATE_SIGNATURE = "VF-IMPORT-2026-SECURE"
TEMPLATE_VERSION = "1.1"


class ProductExcelService:
    """Service pour la gestion des imports/exports Excel de produits."""
    
    # Colonnes du template avec leurs configurations
    COLUMNS = [
        {"key": "name", "header": "Nom du produit *", "width": 35, "required": True},
        {"key": "sku", "header": "Code SKU *", "width": 15, "required": True},
        {"key": "barcode", "header": "Code-barres", "width": 18, "required": False},
        {"key": "category", "header": "Catégorie", "width": 25, "required": False},
        {"key": "brand", "header": "Marque", "width": 15, "required": False},
        {"key": "unit", "header": "Unité", "width": 12, "required": False},
        {"key": "cost_price", "header": "Prix d'achat", "width": 15, "required": False},
        {"key": "selling_price", "header": "Prix de vente *", "width": 15, "required": True},
        {"key": "wholesale_price", "header": "Prix de gros", "width": 15, "required": False},
        {"key": "tax_rate", "header": "Taux TVA (%)", "width": 12, "required": False},
        {"key": "is_taxable", "header": "Taxable (Oui/Non)", "width": 15, "required": False},
        {"key": "track_inventory", "header": "Suivi stock (Oui/Non)", "width": 18, "required": False},
        {"key": "has_expiry_date", "header": "Périssable (Oui/Non)", "width": 18, "required": False},
        {"key": "min_stock_level", "header": "Stock minimum", "width": 14, "required": False},
        {"key": "short_description", "header": "Description", "width": 40, "required": False},
    ]
    
    @classmethod
    def generate_template(cls, organization) -> io.BytesIO:
        """
        Génère un fichier Excel template pour l'importation de produits.
        Le template contient une signature cachée pour validation.
        """
        wb = Workbook()
        
        # =====================================================================
        # FEUILLE 1 : GUIDE DE REMPLISSAGE
        # =====================================================================
        guide_sheet = wb.active
        guide_sheet.title = "📖 Guide"
        
        # Styles pour le guide
        title_font = Font(bold=True, size=16, color="F97316")
        section_font = Font(bold=True, size=12, color="1F2937")
        text_font = Font(size=11, color="374151")
        example_font = Font(size=10, color="6B7280", italic=True)
        warning_font = Font(bold=True, size=11, color="DC2626")
        success_font = Font(bold=True, size=11, color="059669")
        
        guide_sheet.column_dimensions['A'].width = 80
        
        row = 1
        
        # Titre principal
        guide_sheet.cell(row=row, column=1, value="📋 GUIDE D'IMPORTATION DES PRODUITS").font = title_font
        row += 2
        
        # Introduction
        guide_sheet.cell(row=row, column=1, value="Ce fichier vous permet d'importer vos produits en masse dans Vente Facile.").font = text_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="Suivez attentivement les instructions ci-dessous pour éviter les erreurs.").font = text_font
        row += 2
        
        # Section 1 : Feuilles du fichier
        guide_sheet.cell(row=row, column=1, value="📑 STRUCTURE DU FICHIER").font = section_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="• 📖 Guide : Cette feuille d'instructions (vous pouvez la consulter à tout moment)").font = text_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="• 📦 Produits : Feuille principale où vous saisissez vos produits").font = text_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="• 📚 Références : Liste des catégories, marques et unités existantes").font = text_font
        row += 2
        
        # Section 2 : Colonnes obligatoires
        guide_sheet.cell(row=row, column=1, value="⚠️ COLONNES OBLIGATOIRES (marquées d'un *)").font = warning_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="• Nom du produit * : Le nom complet du produit").font = text_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="• Code SKU * : Code unique d'identification (ex: COCA-33CL, FANTA-50CL)").font = text_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="• Prix de vente * : Prix de vente au client (nombre décimal)").font = text_font
        row += 2
        
        # Section 3 : Colonnes optionnelles
        guide_sheet.cell(row=row, column=1, value="📝 COLONNES OPTIONNELLES").font = section_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="• Code-barres : Code-barres EAN/UPC du produit").font = text_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="• Catégorie : Nom de la catégorie (voir feuille Références)").font = text_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="  → Pour une sous-catégorie, utilisez le format : Catégorie parent > Sous-catégorie").font = example_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="  → Exemple : Boissons > Sodas ou Alimentation > Conserves > Légumes").font = example_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="• Marque : Nom de la marque du produit").font = text_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="• Unité : Unité de mesure (Pièce, Kg, Litre, Carton, etc.)").font = text_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="• Prix d'achat : Prix d'achat/coût du produit").font = text_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="• Prix de gros : Prix pour les ventes en gros (optionnel)").font = text_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="• Taux TVA (%) : Pourcentage de TVA (ex: 16 pour 16%)").font = text_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="• Taxable : Oui ou Non").font = text_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="• Suivi stock : Oui ou Non (si Oui, le stock sera géré)").font = text_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="• Périssable : Oui ou Non (si Oui, les dates d'expiration seront gérées)").font = text_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="• Stock minimum : Seuil d'alerte de stock bas").font = text_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="• Description : Description courte du produit").font = text_font
        row += 2
        
        # Section 4 : Création automatique
        guide_sheet.cell(row=row, column=1, value="✨ CRÉATION AUTOMATIQUE DES RÉFÉRENCES").font = success_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="Si vous saisissez une catégorie, marque ou unité qui n'existe pas encore,").font = text_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="elle sera automatiquement créée lors de l'importation !").font = text_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="→ Exemple : Si vous écrivez 'Électronique' comme catégorie et qu'elle n'existe pas,").font = example_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="  elle sera créée automatiquement avant d'importer le produit.").font = example_font
        row += 2
        
        # Section 5 : Règles importantes
        guide_sheet.cell(row=row, column=1, value="🚫 RÈGLES IMPORTANTES").font = warning_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="• Ne modifiez PAS la ligne d'en-tête (ligne 2 de la feuille Produits)").font = text_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="• Ne supprimez PAS la première ligne cachée (signature de validation)").font = text_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="• Chaque SKU doit être UNIQUE (pas de doublons)").font = text_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="• Chaque code-barres doit être UNIQUE (si renseigné)").font = text_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="• Les prix doivent être des nombres (utilisez le point ou la virgule comme séparateur décimal)").font = text_font
        row += 2
        
        # Section 6 : Exemples
        guide_sheet.cell(row=row, column=1, value="📌 EXEMPLES DE SAISIE").font = section_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="Nom: Coca-Cola 33cl | SKU: COCA-33CL | Prix: 500 | Catégorie: Boissons > Sodas").font = example_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="Nom: Riz Oncle Ben's 1kg | SKU: RIZ-OB-1KG | Prix: 2500 | Catégorie: Alimentation").font = example_font
        row += 1
        guide_sheet.cell(row=row, column=1, value="Nom: Savon Palmolive | SKU: SAV-PALM | Prix: 800 | Marque: Palmolive | Unité: Pièce").font = example_font
        row += 2
        
        guide_sheet.cell(row=row, column=1, value="💡 Astuce : Consultez la feuille 'Références' pour voir les catégories, marques et unités existantes.").font = success_font
        
        # =====================================================================
        # FEUILLE 2 : PRODUITS
        # =====================================================================
        ws = wb.create_sheet("📦 Produits")
        
        # Styles
        header_font = Font(bold=True, color="FFFFFF", size=11)
        header_fill = PatternFill(start_color="F97316", end_color="F97316", fill_type="solid")
        header_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        thin_border = Border(
            left=Side(style='thin'),
            right=Side(style='thin'),
            top=Side(style='thin'),
            bottom=Side(style='thin')
        )
        
        # En-têtes (ligne 2, ligne 1 réservée pour la signature)
        for col_idx, col_config in enumerate(cls.COLUMNS, start=1):
            cell = ws.cell(row=2, column=col_idx, value=col_config["header"])
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_alignment
            cell.border = thin_border
            ws.column_dimensions[get_column_letter(col_idx)].width = col_config["width"]
        
        # Ligne 1 : Signature cachée (texte blanc sur fond blanc, protégée)
        signature_data = cls._generate_signature(organization)
        ws.cell(row=1, column=1, value=signature_data)
        ws.cell(row=1, column=1).font = Font(color="FFFFFF", size=1)
        ws.cell(row=1, column=1).fill = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")
        ws.row_dimensions[1].height = 5  # Ligne très petite
        
        # Ajouter des validations de données
        # Oui/Non pour les colonnes booléennes
        yes_no_validation = DataValidation(
            type="list",
            formula1='"Oui,Non"',
            allow_blank=True
        )
        yes_no_validation.error = "Veuillez sélectionner Oui ou Non"
        yes_no_validation.errorTitle = "Valeur invalide"
        
        # Appliquer aux colonnes booléennes
        bool_columns = ["is_taxable", "track_inventory", "has_expiry_date"]
        for col_idx, col_config in enumerate(cls.COLUMNS, start=1):
            if col_config["key"] in bool_columns:
                col_letter = get_column_letter(col_idx)
                yes_no_validation.add(f"{col_letter}3:{col_letter}1000")
        
        ws.add_data_validation(yes_no_validation)
        
        # Charger les catégories avec hiérarchie (parent > enfant)
        categories_with_path = cls._get_categories_with_path(organization)
        brands = list(Brand.objects.filter(organization=organization, is_deleted=False).order_by('name').values_list('name', flat=True))
        units = list(Unit.objects.filter(organization=organization).order_by('name').values_list('name', flat=True))
        
        # =====================================================================
        # FEUILLE 3 : RÉFÉRENCES
        # =====================================================================
        ref_sheet = wb.create_sheet("📚 Références")
        
        # Styles pour les références
        ref_header_font = Font(bold=True, color="FFFFFF", size=11)
        ref_header_fill = PatternFill(start_color="3B82F6", end_color="3B82F6", fill_type="solid")
        ref_header_alignment = Alignment(horizontal="center", vertical="center")
        
        # En-têtes des références
        ref_headers = [
            ("Catégories", 35),
            ("Marques", 25),
            ("Unités", 20),
        ]
        
        for col_idx, (header, width) in enumerate(ref_headers, start=1):
            cell = ref_sheet.cell(row=1, column=col_idx, value=header)
            cell.font = ref_header_font
            cell.fill = ref_header_fill
            cell.alignment = ref_header_alignment
            cell.border = thin_border
            ref_sheet.column_dimensions[get_column_letter(col_idx)].width = width
        
        # Remplir les catégories avec chemin hiérarchique
        for idx, cat_path in enumerate(categories_with_path, start=2):
            ref_sheet.cell(row=idx, column=1, value=cat_path)
        
        # Remplir les marques
        for idx, brand in enumerate(brands, start=2):
            ref_sheet.cell(row=idx, column=2, value=brand)
        
        # Remplir les unités
        for idx, unit in enumerate(units, start=2):
            ref_sheet.cell(row=idx, column=3, value=unit)
        
        # Note explicative en bas
        note_row = max(len(categories_with_path), len(brands), len(units)) + 4
        ref_sheet.cell(row=note_row, column=1, value="💡 Note : Vous pouvez utiliser ces valeurs dans la feuille 'Produits'.").font = Font(italic=True, color="6B7280")
        ref_sheet.cell(row=note_row + 1, column=1, value="Si vous saisissez une valeur qui n'existe pas ici, elle sera créée automatiquement.").font = Font(italic=True, color="6B7280")
        ref_sheet.cell(row=note_row + 2, column=1, value="Pour les sous-catégories, utilisez le format : Catégorie parent > Sous-catégorie").font = Font(italic=True, color="6B7280")
        
        # Figer la première ligne
        ref_sheet.freeze_panes = "A2"
        
        # Validations avec listes déroulantes si des données existent
        if categories_with_path:
            cat_validation = DataValidation(
                type="list",
                formula1=f"'📚 Références'!$A$2:$A${len(categories_with_path) + 1}",
                allow_blank=True
            )
            cat_col = next(i for i, c in enumerate(cls.COLUMNS, start=1) if c["key"] == "category")
            cat_letter = get_column_letter(cat_col)
            cat_validation.add(f"{cat_letter}3:{cat_letter}1000")
            ws.add_data_validation(cat_validation)
        
        if brands:
            brand_validation = DataValidation(
                type="list",
                formula1=f"'📚 Références'!$B$2:$B${len(brands) + 1}",
                allow_blank=True
            )
            brand_col = next(i for i, c in enumerate(cls.COLUMNS, start=1) if c["key"] == "brand")
            brand_letter = get_column_letter(brand_col)
            brand_validation.add(f"{brand_letter}3:{brand_letter}1000")
            ws.add_data_validation(brand_validation)
        
        if units:
            unit_validation = DataValidation(
                type="list",
                formula1=f"'📚 Références'!$C$2:$C${len(units) + 1}",
                allow_blank=True
            )
            unit_col = next(i for i, c in enumerate(cls.COLUMNS, start=1) if c["key"] == "unit")
            unit_letter = get_column_letter(unit_col)
            unit_validation.add(f"{unit_letter}3:{unit_letter}1000")
            ws.add_data_validation(unit_validation)
        
        # Ajouter quelques lignes vides formatées pour guider l'utilisateur
        for row in range(3, 13):
            for col_idx in range(1, len(cls.COLUMNS) + 1):
                cell = ws.cell(row=row, column=col_idx, value="")
                cell.border = thin_border
        
        # Figer les en-têtes
        ws.freeze_panes = "A3"
        
        # Sauvegarder dans un buffer
        buffer = io.BytesIO()
        wb.save(buffer)
        buffer.seek(0)
        
        return buffer
    
    @classmethod
    def _get_categories_with_path(cls, organization) -> List[str]:
        """
        Retourne la liste des catégories avec leur chemin hiérarchique.
        Format: "Parent > Enfant > Sous-enfant"
        """
        categories = Category.objects.filter(
            organization=organization, 
            is_deleted=False
        ).select_related('parent').order_by('parent__name', 'name')
        
        result = []
        
        def get_path(category):
            """Construit le chemin complet d'une catégorie."""
            path_parts = [category.name]
            current = category
            while current.parent:
                path_parts.insert(0, current.parent.name)
                current = current.parent
            return " > ".join(path_parts)
        
        for cat in categories:
            result.append(get_path(cat))
        
        # Trier par chemin
        result.sort()
        return result
    
    @classmethod
    def _generate_signature(cls, organization) -> str:
        """Génère une signature unique pour valider le template."""
        data = f"{TEMPLATE_SIGNATURE}|{TEMPLATE_VERSION}|{organization.id}"
        hash_value = hashlib.sha256(data.encode()).hexdigest()[:16]
        return f"VF|{TEMPLATE_VERSION}|{hash_value}"
    
    @classmethod
    def _validate_signature(cls, signature: str, organization) -> bool:
        """Valide la signature du fichier importé."""
        expected = cls._generate_signature(organization)
        return signature == expected
    
    @classmethod
    def validate_import_file(cls, file_content: bytes, organization) -> Tuple[bool, str, Optional[Workbook]]:
        """
        Valide un fichier Excel avant l'importation.
        Retourne (is_valid, message, workbook)
        """
        try:
            wb = load_workbook(io.BytesIO(file_content))
        except Exception as e:
            return False, f"Fichier Excel invalide: {str(e)}", None
        
        # Vérifier que la feuille "Produits" existe (avec ou sans emoji)
        products_sheet_name = None
        for sheet_name in wb.sheetnames:
            if "Produits" in sheet_name:
                products_sheet_name = sheet_name
                break
        
        if not products_sheet_name:
            return False, "Feuille 'Produits' introuvable. Utilisez le template officiel.", None
        
        ws = wb[products_sheet_name]
        
        # Vérifier la signature (ligne 1, colonne 1)
        signature = ws.cell(row=1, column=1).value
        if not signature or not cls._validate_signature(signature, organization):
            return False, "Ce fichier n'est pas un template valide de Vente Facile. Téléchargez le template officiel.", None
        
        # Vérifier les en-têtes (ligne 2)
        expected_headers = [col["header"] for col in cls.COLUMNS]
        actual_headers = [ws.cell(row=2, column=i).value for i in range(1, len(cls.COLUMNS) + 1)]
        
        if actual_headers != expected_headers:
            return False, "Les en-têtes du fichier ont été modifiés. Utilisez le template officiel sans modifier les en-têtes.", None
        
        return True, "Fichier valide", wb
    
    @classmethod
    def import_products(cls, file_content: bytes, organization, user) -> Dict[str, Any]:
        """
        Importe les produits depuis un fichier Excel.
        Retourne un résumé de l'importation.
        """
        # Valider le fichier
        is_valid, message, wb = cls.validate_import_file(file_content, organization)
        if not is_valid:
            return {
                "success": False,
                "error": message,
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": []
            }
        
        # Trouver la feuille Produits
        products_sheet_name = next((s for s in wb.sheetnames if "Produits" in s), None)
        ws = wb[products_sheet_name]
        
        # Charger les références existantes (avec chemin pour catégories)
        categories_map = cls._build_categories_map(organization)
        brands_map = {b.name.lower(): b for b in Brand.objects.filter(organization=organization, is_deleted=False)}
        units_map = {u.name.lower(): u for u in Unit.objects.filter(organization=organization)}
        
        # Charger les SKU et codes-barres existants pour détecter les doublons
        existing_skus = set(Product.objects.filter(organization=organization, is_deleted=False).values_list('sku', flat=True))
        existing_barcodes = set(Product.objects.filter(organization=organization, is_deleted=False, barcode__isnull=False).exclude(barcode='').values_list('barcode', flat=True))
        
        results = {
            "success": True,
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "errors": []
        }
        
        products_to_create = []
        row_num = 3  # Données commencent à la ligne 3
        
        while True:
            # Lire la ligne
            row_data = {}
            has_data = False
            
            for col_idx, col_config in enumerate(cls.COLUMNS, start=1):
                value = ws.cell(row=row_num, column=col_idx).value
                if value is not None and str(value).strip():
                    has_data = True
                row_data[col_config["key"]] = value
            
            if not has_data:
                break  # Fin des données
            
            # Valider les champs requis
            errors = []
            
            name = str(row_data.get("name") or "").strip()
            sku = str(row_data.get("sku") or "").strip()
            selling_price = row_data.get("selling_price")
            
            if not name:
                errors.append("Nom du produit requis")
            if not sku:
                errors.append("Code SKU requis")
            if selling_price is None:
                errors.append("Prix de vente requis")
            
            # Vérifier les doublons SKU
            if sku and sku in existing_skus:
                errors.append(f"SKU '{sku}' existe déjà")
            
            # Vérifier les doublons code-barres
            barcode = str(row_data.get("barcode") or "").strip()
            if barcode and barcode in existing_barcodes:
                errors.append(f"Code-barres '{barcode}' existe déjà")
            
            if errors:
                results["errors"].append({
                    "row": row_num,
                    "name": name or f"Ligne {row_num}",
                    "errors": errors
                })
                results["skipped"] += 1
                row_num += 1
                continue
            
            # Préparer le produit
            try:
                product_data = cls._parse_row_data(row_data, organization, user)
                product_data["organization"] = organization
                product_data["created_by"] = user
                product_data["slug"] = slugify(name)
                
                # Assurer l'unicité du slug
                base_slug = product_data["slug"]
                counter = 1
                while Product.objects.filter(organization=organization, slug=product_data["slug"], is_deleted=False).exists():
                    product_data["slug"] = f"{base_slug}-{counter}"
                    counter += 1
                
                products_to_create.append(product_data)
                existing_skus.add(sku)  # Ajouter pour éviter les doublons dans le même fichier
                if barcode:
                    existing_barcodes.add(barcode)
                
            except Exception as e:
                results["errors"].append({
                    "row": row_num,
                    "name": name,
                    "errors": [str(e)]
                })
                results["skipped"] += 1
            
            row_num += 1
        
        # Créer les produits en transaction
        if products_to_create:
            try:
                with transaction.atomic():
                    for product_data in products_to_create:
                        Product.objects.create(**product_data)
                        results["created"] += 1
            except Exception as e:
                results["success"] = False
                results["error"] = f"Erreur lors de la création des produits: {str(e)}"
                results["created"] = 0
        
        return results
    
    @classmethod
    def _build_categories_map(cls, organization) -> Dict[str, Category]:
        """
        Construit un dictionnaire des catégories avec leur chemin comme clé.
        Supporte les formats: "Catégorie" ou "Parent > Enfant > Sous-enfant"
        """
        categories = Category.objects.filter(
            organization=organization, 
            is_deleted=False
        ).select_related('parent__parent')
        
        result = {}
        
        for cat in categories:
            # Ajouter par nom simple (en minuscules)
            result[cat.name.lower()] = cat
            
            # Ajouter par chemin complet
            path_parts = [cat.name]
            current = cat
            while current.parent:
                path_parts.insert(0, current.parent.name)
                current = current.parent
            full_path = " > ".join(path_parts).lower()
            result[full_path] = cat
        
        return result
    
    @classmethod
    def _get_or_create_category(cls, category_input: str, organization, user) -> Optional[Category]:
        """
        Récupère ou crée une catégorie à partir d'une chaîne.
        Supporte le format "Parent > Enfant > Sous-enfant".
        """
        if not category_input or not category_input.strip():
            return None
        
        category_input = category_input.strip()
        
        # Vérifier si c'est un chemin hiérarchique
        if " > " in category_input:
            parts = [p.strip() for p in category_input.split(" > ")]
            parent = None
            
            for part in parts:
                if not part:
                    continue
                    
                # Chercher la catégorie existante
                query = Category.objects.filter(
                    organization=organization,
                    name__iexact=part,
                    is_deleted=False
                )
                if parent:
                    query = query.filter(parent=parent)
                else:
                    query = query.filter(parent__isnull=True)
                
                category = query.first()
                
                if not category:
                    # Créer la catégorie
                    category = Category.objects.create(
                        organization=organization,
                        name=part,
                        slug=slugify(part),
                        parent=parent,
                        created_by=user
                    )
                
                parent = category
            
            return parent
        else:
            # Catégorie simple (sans hiérarchie)
            category = Category.objects.filter(
                organization=organization,
                name__iexact=category_input,
                is_deleted=False
            ).first()
            
            if not category:
                category = Category.objects.create(
                    organization=organization,
                    name=category_input,
                    slug=slugify(category_input),
                    created_by=user
                )
            
            return category
    
    @classmethod
    def _get_or_create_brand(cls, brand_name: str, organization, user) -> Optional[Brand]:
        """Récupère ou crée une marque."""
        if not brand_name or not brand_name.strip():
            return None
        
        brand_name = brand_name.strip()
        brand = Brand.objects.filter(
            organization=organization,
            name__iexact=brand_name,
            is_deleted=False
        ).first()
        
        if not brand:
            brand = Brand.objects.create(
                organization=organization,
                name=brand_name,
                slug=slugify(brand_name),
                created_by=user
            )
        
        return brand
    
    @classmethod
    def _get_or_create_unit(cls, unit_name: str, organization) -> Optional[Unit]:
        """Récupère ou crée une unité."""
        if not unit_name or not unit_name.strip():
            return None
        
        unit_name = unit_name.strip()
        unit = Unit.objects.filter(
            organization=organization,
            name__iexact=unit_name
        ).first()
        
        if not unit:
            # Créer l'unité avec un symbole par défaut
            symbol = unit_name[:3].upper()
            unit = Unit.objects.create(
                organization=organization,
                name=unit_name,
                symbol=symbol
            )
        
        return unit
    
    @classmethod
    def _parse_row_data(cls, row_data: Dict, organization, user) -> Dict:
        """Parse les données d'une ligne Excel en données de produit."""
        
        def parse_decimal(value, default=Decimal("0.00")):
            if value is None:
                return default
            try:
                return Decimal(str(value).replace(",", ".").strip())
            except (InvalidOperation, ValueError):
                return default
        
        def parse_bool(value, default=True):
            if value is None:
                return default
            str_val = str(value).lower().strip()
            if str_val in ("oui", "yes", "1", "true", "vrai"):
                return True
            if str_val in ("non", "no", "0", "false", "faux"):
                return False
            return default
        
        def parse_int(value, default=0):
            if value is None:
                return default
            try:
                return int(float(str(value).strip()))
            except (ValueError, TypeError):
                return default
        
        # Récupérer ou créer les références
        category_input = str(row_data.get("category") or "").strip()
        brand_input = str(row_data.get("brand") or "").strip()
        unit_input = str(row_data.get("unit") or "").strip()
        
        category = cls._get_or_create_category(category_input, organization, user) if category_input else None
        brand = cls._get_or_create_brand(brand_input, organization, user) if brand_input else None
        unit = cls._get_or_create_unit(unit_input, organization) if unit_input else None
        
        return {
            "name": str(row_data.get("name") or "").strip(),
            "sku": str(row_data.get("sku") or "").strip(),
            "barcode": str(row_data.get("barcode") or "").strip(),
            "category": category,
            "brand": brand,
            "unit": unit,
            "cost_price": parse_decimal(row_data.get("cost_price")),
            "selling_price": parse_decimal(row_data.get("selling_price")),
            "wholesale_price": parse_decimal(row_data.get("wholesale_price")) or None,
            "tax_rate": parse_decimal(row_data.get("tax_rate")),
            "is_taxable": parse_bool(row_data.get("is_taxable"), True),
            "track_inventory": parse_bool(row_data.get("track_inventory"), True),
            "has_expiry_date": parse_bool(row_data.get("has_expiry_date"), False),
            "min_stock_level": parse_int(row_data.get("min_stock_level")),
            "short_description": str(row_data.get("short_description") or "").strip(),
            "is_active": True,
        }
    
    @classmethod
    def check_duplicate(cls, organization, sku: str = None, barcode: str = None, exclude_id=None) -> Dict[str, Any]:
        """
        Vérifie si un produit avec le même SKU ou code-barres existe déjà.
        Utilisé pour la création normale de produit aussi.
        """
        duplicates = {"sku": None, "barcode": None}
        
        if sku:
            sku_query = Product.objects.filter(organization=organization, sku=sku, is_deleted=False)
            if exclude_id:
                sku_query = sku_query.exclude(id=exclude_id)
            existing = sku_query.first()
            if existing:
                duplicates["sku"] = {"id": str(existing.id), "name": existing.name}
        
        if barcode and barcode.strip():
            barcode_query = Product.objects.filter(organization=organization, barcode=barcode, is_deleted=False)
            if exclude_id:
                barcode_query = barcode_query.exclude(id=exclude_id)
            existing = barcode_query.first()
            if existing:
                duplicates["barcode"] = {"id": str(existing.id), "name": existing.name}
        
        return duplicates
