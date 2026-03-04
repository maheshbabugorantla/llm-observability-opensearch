from flask import Flask, request, jsonify
import os
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from openai import OpenAI
from anthropic import Anthropic
from traceloop.sdk import Traceloop
from traceloop.sdk.decorators import workflow, task
from dotenv import load_dotenv
from pythonjsonlogger import jsonlogger


# Load environment variables from .env file
load_dotenv()


# Configure JSON logging
def setup_json_logging():
    """Configure structured JSON logging for the application"""
    log_handler = logging.StreamHandler(sys.stdout)

    # Create JSON formatter with custom fields
    formatter = jsonlogger.JsonFormatter(
        '%(asctime)s %(name)s %(levelname)s %(message)s %(pathname)s %(lineno)d',
        rename_fields={
            "asctime": "timestamp",
            "levelname": "level",
            "pathname": "file",
            "lineno": "line"
        }
    )

    log_handler.setFormatter(formatter)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.addHandler(log_handler)
    root_logger.setLevel(logging.INFO)

    # Also configure Flask's logger
    flask_logger = logging.getLogger('werkzeug')
    flask_logger.setLevel(logging.INFO)

    return root_logger

# Setup logging before initializing other components
logger = setup_json_logging()
logger.info("Initializing Recipe Generator Service", extra={
    "service": "recipe-generator",
    "version": "1.0.0",
    "environment": os.getenv("ENVIRONMENT", "development")
})

# Initialize Traceloop for OpenLLMetry observability
Traceloop.init(
    app_name="recipe-generator-service",
    api_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318"),
    disable_batch=False
)


app = Flask(__name__)

# Initialize LLM clients
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
anthropic_client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


@task(name="generate_recipe_prompt")
def generate_recipe_prompt(dish_name, cuisine_type, dietary_restrictions, servings):
    """Generate a recipe generation prompt based on user preferences"""
    prompt = f"""Create a detailed recipe for {dish_name}"""

    if cuisine_type:
        prompt += f" in {cuisine_type} style"

    prompt += f" that serves {servings} people."

    if dietary_restrictions:
        prompt += f"\n\nDietary restrictions: {', '.join(dietary_restrictions)}"

    prompt += """

Please include:
1. Ingredient list with measurements
2. Step-by-step cooking instructions
3. Estimated preparation and cooking time
4. Nutritional information (calories, protein, carbs, fats)
5. Tips for best results
"""
    return prompt


@task(name="call_openai")
def call_openai(prompt, model="gpt-5-mini"):
    """Call OpenAI API for recipe generation"""
    logger.info("Calling OpenAI API", extra={
        "provider": "openai",
        "model": model,
        "prompt_length": len(prompt)
    })

    try:
        response = openai_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are an expert chef and nutritionist who creates delicious, healthy recipes."},
                {"role": "user", "content": prompt}
            ],
            max_completion_tokens=2000
        )

        result = {
            "recipe": response.choices[0].message.content,
            "model": response.model,
            "usage": {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens
            }
        }

        logger.info("OpenAI API call successful", extra={
            "provider": "openai",
            "model": response.model,
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_tokens": response.usage.total_tokens
        })

        return result
    except Exception as e:
        logger.error("OpenAI API call failed", extra={
            "provider": "openai",
            "model": model,
            "error": str(e),
            "error_type": type(e).__name__
        })
        raise


@task(name="call_claude")
def call_claude(prompt, model="claude-sonnet-4-5-20250929", temperature=0.7):
    """Call Anthropic Claude API for recipe generation"""
    logger.info("Calling Claude API", extra={
        "provider": "anthropic",
        "model": model,
        "temperature": temperature,
        "prompt_length": len(prompt)
    })

    try:
        response = anthropic_client.messages.create(
            model=model,
            max_tokens=2000,
            temperature=temperature,
            system="You are an expert chef and nutritionist who creates delicious, healthy recipes.",
            messages=[
                {"role": "user", "content": prompt}
            ]
        )

        result = {
            "recipe": response.content[0].text,
            "model": response.model,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens
            }
        }

        logger.info("Claude API call successful", extra={
            "provider": "anthropic",
            "model": response.model,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "total_tokens": response.usage.input_tokens + response.usage.output_tokens
        })

        return result
    except Exception as e:
        logger.error("Claude API call failed", extra={
            "provider": "anthropic",
            "model": model,
            "error": str(e),
            "error_type": type(e).__name__
        })
        raise


@workflow(name="recipe_generation_workflow")
def generate_recipe(provider, dish_name, cuisine_type, dietary_restrictions, servings, model=None, temperature=0.7):
    """Main workflow for recipe generation"""
    logger.info("Starting recipe generation workflow", extra={
        "workflow": "recipe_generation",
        "provider": provider,
        "dish_name": dish_name,
        "cuisine_type": cuisine_type,
        "servings": servings,
        "has_dietary_restrictions": len(dietary_restrictions) > 0
    })

    # Generate the prompt
    prompt = generate_recipe_prompt(dish_name, cuisine_type, dietary_restrictions, servings)

    # Route to appropriate LLM provider
    if provider.lower() == "openai":
        model = model or "gpt-5-mini"
        result = call_openai(prompt, model=model)
    elif provider.lower() == "claude" or provider.lower() == "anthropic":
        model = model or "claude-sonnet-4-5-20250929"
        result = call_claude(prompt, model=model, temperature=temperature)
    else:
        logger.error("Unsupported provider specified", extra={
            "workflow": "recipe_generation",
            "provider": provider,
            "dish_name": dish_name
        })
        raise ValueError(f"Unsupported provider: {provider}. Use 'openai' or 'claude'.")

    result["provider"] = provider
    result["prompt"] = prompt

    logger.info("Recipe generation workflow completed", extra={
        "workflow": "recipe_generation",
        "provider": provider,
        "dish_name": dish_name,
        "model_used": result.get("model")
    })

    return result


@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint"""
    return jsonify({"status": "healthy", "service": "recipe-generator"}), 200


@app.route("/recipe/generate", methods=["POST"])
def generate_recipe_endpoint():
    """
    Generate a recipe using specified LLM provider

    Expected JSON payload:
    {
        "provider": "openai" | "claude",
        "dish_name": "Chicken Tikka Masala",
        "cuisine_type": "Indian",  // optional
        "dietary_restrictions": ["gluten-free", "dairy-free"],  // optional
        "servings": 4,
        "model": "gpt-4",  // optional, provider-specific model
        "temperature": 0.7  // optional
    }
    """
    logger.info("Received recipe generation request", extra={
        "endpoint": "/recipe/generate",
        "method": "POST",
        "remote_addr": request.remote_addr
    })

    try:
        data = request.get_json()

        # Validate required fields
        if not data:
            logger.warning("Invalid request: empty body", extra={
                "endpoint": "/recipe/generate",
                "remote_addr": request.remote_addr
            })
            return jsonify({"error": "Request body must be JSON"}), 400

        provider = data.get("provider")
        dish_name = data.get("dish_name")

        if not provider:
            logger.warning("Invalid request: missing provider", extra={
                "endpoint": "/recipe/generate",
                "remote_addr": request.remote_addr
            })
            return jsonify({"error": "Missing required field: provider"}), 400
        if not dish_name:
            logger.warning("Invalid request: missing dish_name", extra={
                "endpoint": "/recipe/generate",
                "remote_addr": request.remote_addr,
                "provider": provider
            })
            return jsonify({"error": "Missing required field: dish_name"}), 400

        # Optional fields with defaults
        cuisine_type = data.get("cuisine_type", "")
        dietary_restrictions = data.get("dietary_restrictions", [])
        servings = data.get("servings", 4)
        model = data.get("model")
        temperature = data.get("temperature", 0.7)

        # Generate recipe
        result = generate_recipe(
            provider=provider,
            dish_name=dish_name,
            cuisine_type=cuisine_type,
            dietary_restrictions=dietary_restrictions,
            servings=servings,
            model=model,
            temperature=temperature
        )

        logger.info("Recipe generation request completed successfully", extra={
            "endpoint": "/recipe/generate",
            "provider": provider,
            "dish_name": dish_name,
            "model": result.get("model"),
            "remote_addr": request.remote_addr
        })

        return jsonify({
            "success": True,
            "data": result
        }), 200

    except ValueError as e:
        logger.error("Validation error in recipe generation", extra={
            "endpoint": "/recipe/generate",
            "error": str(e),
            "error_type": "ValueError",
            "remote_addr": request.remote_addr
        })
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error("Error generating recipe", extra={
            "endpoint": "/recipe/generate",
            "error": str(e),
            "error_type": type(e).__name__,
            "remote_addr": request.remote_addr
        })
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@app.route("/recipe/compare", methods=["POST"])
@workflow(name="recipe_comparison_workflow")
def compare_recipes_endpoint():
    """
    Generate the same recipe using both OpenAI and Claude for comparison

    Expected JSON payload:
    {
        "dish_name": "Pasta Carbonara",
        "cuisine_type": "Italian",  // optional
        "dietary_restrictions": [],  // optional
        "servings": 4,
        "temperature": 0.7  // optional
    }
    """
    logger.info("Received recipe comparison request", extra={
        "endpoint": "/recipe/compare",
        "method": "POST",
        "remote_addr": request.remote_addr
    })

    try:
        data = request.get_json()

        if not data:
            logger.warning("Invalid request: empty body", extra={
                "endpoint": "/recipe/compare",
                "remote_addr": request.remote_addr
            })
            return jsonify({"error": "Request body must be JSON"}), 400

        dish_name = data.get("dish_name")
        if not dish_name:
            logger.warning("Invalid request: missing dish_name", extra={
                "endpoint": "/recipe/compare",
                "remote_addr": request.remote_addr
            })
            return jsonify({"error": "Missing required field: dish_name"}), 400

        cuisine_type = data.get("cuisine_type", "")
        dietary_restrictions = data.get("dietary_restrictions", [])
        servings = data.get("servings", 4)
        temperature = data.get("temperature", 0.7)

        logger.info("Starting recipe comparison", extra={
            "endpoint": "/recipe/compare",
            "dish_name": dish_name,
            "cuisine_type": cuisine_type,
            "servings": servings
        })

        # Generate with OpenAI
        openai_result = generate_recipe(
            provider="openai",
            dish_name=dish_name,
            cuisine_type=cuisine_type,
            dietary_restrictions=dietary_restrictions,
            servings=servings,
            temperature=temperature
        )

        # Generate with Claude
        claude_result = generate_recipe(
            provider="claude",
            dish_name=dish_name,
            cuisine_type=cuisine_type,
            dietary_restrictions=dietary_restrictions,
            servings=servings,
            temperature=temperature
        )

        logger.info("Recipe comparison completed successfully", extra={
            "endpoint": "/recipe/compare",
            "dish_name": dish_name,
            "openai_model": openai_result.get("model"),
            "claude_model": claude_result.get("model"),
            "remote_addr": request.remote_addr
        })

        return jsonify({
            "success": True,
            "data": {
                "openai": openai_result,
                "claude": claude_result
            }
        }), 200

    except Exception as e:
        logger.error("Error comparing recipes", extra={
            "endpoint": "/recipe/compare",
            "error": str(e),
            "error_type": type(e).__name__,
            "remote_addr": request.remote_addr
        })
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@app.route("/recipe/batch", methods=["POST"])
@workflow(name="batch_recipe_generation")
def batch_generate_endpoint():
    """
    Generate multiple recipes in a single request

    Expected JSON payload:
    {
        "provider": "openai" | "claude",
        "recipes": [
            {
                "dish_name": "Spaghetti Carbonara",
                "cuisine_type": "Italian",
                "servings": 4
            },
            {
                "dish_name": "Chicken Tikka Masala",
                "cuisine_type": "Indian",
                "servings": 6
            }
        ],
        "temperature": 0.7  // optional
    }
    """
    logger.info("Received batch recipe generation request", extra={
        "endpoint": "/recipe/batch",
        "method": "POST",
        "remote_addr": request.remote_addr
    })

    try:
        data = request.get_json()

        if not data:
            logger.warning("Invalid request: empty body", extra={
                "endpoint": "/recipe/batch",
                "remote_addr": request.remote_addr
            })
            return jsonify({"error": "Request body must be JSON"}), 400

        provider = data.get("provider")
        recipes_input = data.get("recipes", [])
        temperature = data.get("temperature", 0.7)

        if not provider:
            logger.warning("Invalid request: missing provider", extra={
                "endpoint": "/recipe/batch",
                "remote_addr": request.remote_addr
            })
            return jsonify({"error": "Missing required field: provider"}), 400
        if not recipes_input:
            logger.warning("Invalid request: missing recipes", extra={
                "endpoint": "/recipe/batch",
                "provider": provider,
                "remote_addr": request.remote_addr
            })
            return jsonify({"error": "Missing required field: recipes"}), 400

        logger.info("Starting batch recipe generation", extra={
            "endpoint": "/recipe/batch",
            "provider": provider,
            "recipe_count": len(recipes_input),
            "remote_addr": request.remote_addr
        })

        results = []
        for idx, recipe_data in enumerate(recipes_input):
            dish_name = recipe_data.get("dish_name")
            if not dish_name:
                logger.warning("Skipping recipe without dish_name", extra={
                    "endpoint": "/recipe/batch",
                    "batch_index": idx,
                    "provider": provider
                })
                continue

            result = generate_recipe(
                provider=provider,
                dish_name=dish_name,
                cuisine_type=recipe_data.get("cuisine_type", ""),
                dietary_restrictions=recipe_data.get("dietary_restrictions", []),
                servings=recipe_data.get("servings", 4),
                temperature=temperature
            )
            results.append(result)

        logger.info("Batch recipe generation completed", extra={
            "endpoint": "/recipe/batch",
            "provider": provider,
            "requested_count": len(recipes_input),
            "completed_count": len(results),
            "remote_addr": request.remote_addr
        })

        return jsonify({
            "success": True,
            "count": len(results),
            "data": results
        }), 200

    except Exception as e:
        logger.error("Error in batch generation", extra={
            "endpoint": "/recipe/batch",
            "error": str(e),
            "error_type": type(e).__name__,
            "remote_addr": request.remote_addr
        })
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


# ============================================================================
# MULTI-AGENTIC RESTAURANT MENU DESIGNER
# Demonstrates: Parallel Execution, Nested Workflows, Decision Trees, Error Handling
# ============================================================================

# Agent 1: Menu Coordinator (GPT-5 Mini) - Strategic planning and validation
@task(name="coordinator_plan_menu_structure")
def coordinator_plan_menu_structure(cuisine, menu_type, courses, dietary_requirements, budget, season, occasion):
    """
    Coordinator Agent: Plans the overall menu structure
    Uses GPT-4 for strategic decision-making
    Decision Tree: Determines course types based on inputs
    """
    logger.info("Coordinator planning menu structure", extra={
        "agent": "coordinator",
        "cuisine": cuisine,
        "courses": courses,
        "menu_type": menu_type
    })

    prompt = f"""You are an expert Menu Coordinator for a {menu_type} restaurant.

Plan a {courses}-course {cuisine} menu for a {occasion}.

Requirements:
- Budget: {budget}
- Season: {season}
- Dietary: {', '.join(dietary_requirements) if dietary_requirements else 'None'}

Provide:
1. Course structure (e.g., Appetizer, Soup, Main, Dessert)
2. Theme/concept for each course
3. Key ingredients to feature
4. Overall menu narrative

Format as JSON with structure:
{{
  "menu_concept": "overall theme",
  "courses": [
    {{"course_number": 1, "type": "Appetizer", "theme": "...", "key_ingredients": []}},
    ...
  ]
}}"""

    try:
        response = openai_client.chat.completions.create(
            model="gpt-5-mini",
            messages=[
                {"role": "system", "content": "You are an expert menu coordinator. Respond only with valid JSON."},
                {"role": "user", "content": prompt}
            ],
            max_completion_tokens=1500
        )

        import json
        menu_plan = json.loads(response.choices[0].message.content)

        logger.info("Coordinator menu plan complete", extra={
            "agent": "coordinator",
            "courses_planned": len(menu_plan.get("courses", [])),
            "concept": menu_plan.get("menu_concept", "")
        })

        return menu_plan

    except Exception as e:
        logger.error("Coordinator planning failed", extra={
            "agent": "coordinator",
            "error": str(e)
        })
        # Fallback simple structure
        return {
            "menu_concept": f"Classic {cuisine} {menu_type} experience",
            "courses": [
                {"course_number": i+1, "type": f"Course {i+1}", "theme": cuisine, "key_ingredients": []}
                for i in range(courses)
            ]
        }


# Agent 2: Executive Chef (Claude) - Recipe creation
@task(name="chef_create_course_recipe")
def chef_create_course_recipe(course_info, dietary_requirements, iteration=1, feedback=None):
    """
    Chef Agent: Creates detailed recipes for each course
    Uses Claude for creative recipe generation
    Error Handling: Supports retry with feedback
    """
    course_type = course_info.get("type", "Course")
    theme = course_info.get("theme", "")
    key_ingredients = course_info.get("key_ingredients", [])

    logger.info("Chef creating recipe", extra={
        "agent": "chef",
        "course_type": course_type,
        "iteration": iteration,
        "has_feedback": feedback is not None
    })

    feedback_text = f"\n\nPREVIOUS FEEDBACK TO ADDRESS:\n{feedback}" if feedback else ""

    prompt = f"""You are an Executive Chef creating a {course_type} recipe.

Theme: {theme}
Key Ingredients: {', '.join(key_ingredients) if key_ingredients else 'chef choice'}
Dietary Requirements: {', '.join(dietary_requirements) if dietary_requirements else 'None'}
{feedback_text}

Create a detailed recipe including:
1. Dish name
2. Ingredient list with quantities (for 2 servings)
3. Cooking instructions (brief, 3-5 steps)
4. Estimated prep/cook time
5. Plating suggestion

Keep it elegant and restaurant-quality."""

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=2000,
            temperature=0.8 if iteration == 1 else 0.6,  # Lower temp on retry
            system="You are an Executive Chef at a fine dining restaurant. Be creative but practical.",
            messages=[{"role": "user", "content": prompt}]
        )

        recipe_text = response.content[0].text

        logger.info("Chef recipe created", extra={
            "agent": "chef",
            "course_type": course_type,
            "iteration": iteration,
            "recipe_length": len(recipe_text)
        })

        return {
            "course_type": course_type,
            "recipe": recipe_text,
            "iteration": iteration,
            "model": response.model,
            "tokens": response.usage.input_tokens + response.usage.output_tokens
        }

    except Exception as e:
        logger.error("Chef recipe creation failed", extra={
            "agent": "chef",
            "course_type": course_type,
            "iteration": iteration,
            "error": str(e)
        })
        raise


# Agent 3: Sommelier (GPT-5 Nano) - Wine pairing
@task(name="sommelier_pair_wine_with_course")
def sommelier_pair_wine_with_course(course_recipe, budget, attempt=1):
    """
    Sommelier Agent: Suggests wine pairings
    Uses GPT-4 for wine expertise
    Retry Logic: Max 2 attempts if pairing doesn't match
    """
    course_type = course_recipe.get("course_type", "Unknown")
    recipe_text = course_recipe.get("recipe", "")

    logger.info("Sommelier pairing wine", extra={
        "agent": "sommelier",
        "course_type": course_type,
        "attempt": attempt,
        "budget": budget
    })

    # Extract dish name from recipe (first line usually)
    dish_name = recipe_text.split('\n')[0] if recipe_text else course_type

    prompt = f"""You are a Master Sommelier at a fine dining restaurant.

Course: {course_type}
Dish: {dish_name}
Recipe excerpt: {recipe_text[:300]}...

Budget: {budget}
Attempt: {attempt} {"(refining previous pairing)" if attempt > 1 else ""}

Suggest ONE specific wine pairing including:
1. Wine name and vintage (be specific)
2. Region/Producer
3. Why it pairs well (flavor profile matching)
4. Serving temperature
5. Price range estimate

Be concise but informative."""

    try:
        response = openai_client.chat.completions.create(
            model="gpt-5-nano",
            messages=[
                {"role": "system", "content": "You are a Master Sommelier. Provide specific, accurate wine recommendations."},
                {"role": "user", "content": prompt}
            ],
            max_completion_tokens=500
        )

        pairing_text = response.choices[0].message.content

        # Simple validation: check if wine name is mentioned
        has_wine_name = any(word in pairing_text.lower() for word in ['wine', 'vintage', 'region', 'producer'])

        if not has_wine_name and attempt < 2:
            logger.warning("Sommelier pairing incomplete, will retry", extra={
                "agent": "sommelier",
                "course_type": course_type,
                "attempt": attempt
            })
            # Simulate retry by raising exception
            raise ValueError("Incomplete wine pairing - missing key details")

        logger.info("Sommelier pairing complete", extra={
            "agent": "sommelier",
            "course_type": course_type,
            "attempt": attempt,
            "pairing_length": len(pairing_text)
        })

        return {
            "course_type": course_type,
            "wine_pairing": pairing_text,
            "attempt": attempt,
            "model": response.model,
            "tokens": response.usage.total_tokens
        }

    except Exception as e:
        logger.error("Sommelier pairing failed", extra={
            "agent": "sommelier",
            "course_type": course_type,
            "attempt": attempt,
            "error": str(e)
        })

        if attempt < 2:
            # Retry logic
            logger.info("Sommelier retrying wine pairing", extra={
                "agent": "sommelier",
                "course_type": course_type,
                "next_attempt": attempt + 1
            })
            time.sleep(1)  # Brief pause before retry
            return sommelier_pair_wine_with_course(course_recipe, budget, attempt=attempt+1)
        else:
            # Max retries reached, return fallback
            return {
                "course_type": course_type,
                "wine_pairing": f"House recommendation for {course_type}",
                "attempt": attempt,
                "model": "fallback",
                "tokens": 0
            }


# Agent 4: Nutritionist (Claude) - Health analysis
@task(name="nutritionist_analyze_course")
def nutritionist_analyze_course(course_recipe, dietary_requirements):
    """
    Nutritionist Agent: Analyzes nutritional content and compliance
    Uses Claude for detailed health analysis
    Decision Tree: Approve or suggest modifications
    """
    course_type = course_recipe.get("course_type", "Unknown")
    recipe_text = course_recipe.get("recipe", "")

    logger.info("Nutritionist analyzing course", extra={
        "agent": "nutritionist",
        "course_type": course_type,
        "dietary_requirements": dietary_requirements
    })

    prompt = f"""You are a Restaurant Nutritionist ensuring menu items meet dietary standards.

Course: {course_type}
Recipe:
{recipe_text}

Dietary Requirements to check: {', '.join(dietary_requirements) if dietary_requirements else 'General healthiness'}

Analyze:
1. Nutritional balance (protein, carbs, fats, veggies)
2. Compliance with dietary requirements
3. Healthiness rating (1-10)
4. Suggested modifications (if any)

Respond in format:
APPROVED: Yes/No
RATING: X/10
FEEDBACK: (brief notes)
MODIFICATIONS: (if needed, else "None")"""

    try:
        response = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            temperature=0.4,  # Lower temp for analytical task
            system="You are a professional nutritionist analyzing restaurant recipes.",
            messages=[{"role": "user", "content": prompt}]
        )

        analysis_text = response.content[0].text

        # Parse approval status
        is_approved = "APPROVED: Yes" in analysis_text or "approved" in analysis_text.lower()
        has_modifications = "MODIFICATIONS:" in analysis_text and "None" not in analysis_text.split("MODIFICATIONS:")[1][:50]

        logger.info("Nutritionist analysis complete", extra={
            "agent": "nutritionist",
            "course_type": course_type,
            "approved": is_approved,
            "has_modifications": has_modifications
        })

        return {
            "course_type": course_type,
            "analysis": analysis_text,
            "approved": is_approved,
            "needs_modification": has_modifications,
            "model": response.model,
            "tokens": response.usage.input_tokens + response.usage.output_tokens
        }

    except Exception as e:
        logger.error("Nutritionist analysis failed", extra={
            "agent": "nutritionist",
            "course_type": course_type,
            "error": str(e)
        })
        # Fallback: approve to avoid blocking workflow
        return {
            "course_type": course_type,
            "analysis": "Analysis unavailable",
            "approved": True,
            "needs_modification": False,
            "model": "fallback",
            "tokens": 0
        }


# Sub-workflow 1: Parallel Agent Research
@workflow(name="parallel_agent_research_workflow")
def parallel_agent_research(menu_plan, dietary_requirements, budget):
    """
    Parallel Execution: Chef, Nutritionist initial research, and Sommelier research run concurrently
    Demonstrates multi-agent parallel processing
    """
    logger.info("Starting parallel agent research", extra={
        "workflow": "parallel_research",
        "num_courses": len(menu_plan.get("courses", []))
    })

    courses = menu_plan.get("courses", [])

    def research_chef_courses():
        """Chef creates initial recipes for all courses"""
        recipes = []
        for course in courses:
            recipe = chef_create_course_recipe(course, dietary_requirements, iteration=1)
            recipes.append(recipe)
        return recipes

    def research_nutrition_requirements():
        """Nutritionist analyzes dietary requirements"""
        prompt = f"As a nutritionist, provide guidelines for: {', '.join(dietary_requirements) if dietary_requirements else 'general healthy eating'}"
        try:
            response = anthropic_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=500,
                temperature=0.3,
                messages=[{"role": "user", "content": prompt}]
            )
            return response.content[0].text
        except Exception as e:
            logger.error("Nutritionist analysis failed", extra={
                "agent": "nutritionist",
                "error": str(e),
                "error_type": type(e).__name__
            })
            logger.exception(e)
            return "Standard nutrition guidelines"

    def research_wine_strategy():
        """Sommelier researches wine pairing strategy"""
        prompt = f"As a sommelier, provide a wine pairing strategy for a {menu_plan.get('menu_concept', 'multi-course')} menu within {budget} budget"
        try:
            response = openai_client.chat.completions.create(
                model="gpt-5-nano",
                messages=[{"role": "user", "content": prompt}],
                max_completion_tokens=500
            )
            return response.choices[0].message.content
        except Exception as e:
            logger.error("Sommelier pairing strategy failed", extra={
                "agent": "sommelier",
                "error": str(e),
                "error_type": type(e).__name__
            })
            logger.exception(e)
            return "Classic pairing principles"

    # Execute in parallel using ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=3) as executor:
        future_chef = executor.submit(research_chef_courses)
        future_nutrition = executor.submit(research_nutrition_requirements)
        future_wine = executor.submit(research_wine_strategy)

        # Wait for all to complete
        chef_recipes = future_chef.result()
        nutrition_guidelines = future_nutrition.result()
        wine_strategy = future_wine.result()

    logger.info("Parallel agent research completed", extra={
        "workflow": "parallel_research",
        "recipes_created": len(chef_recipes),
        "has_nutrition_guidelines": bool(nutrition_guidelines),
        "has_wine_strategy": bool(wine_strategy)
    })

    return {
        "recipes": chef_recipes,
        "nutrition_guidelines": nutrition_guidelines,
        "wine_strategy": wine_strategy
    }


# Sub-workflow 2: Recipe Refinement with Nutritionist Feedback
@workflow(name="recipe_refinement_workflow")
def refine_recipes_with_feedback(initial_recipes, dietary_requirements):
    """
    Nested Workflow + Error Handling: Chef refines recipes based on nutritionist feedback
    Demonstrates iterative improvement with retry logic
    """
    logger.info("Starting recipe refinement", extra={
        "workflow": "recipe_refinement",
        "num_recipes": len(initial_recipes)
    })

    refined_recipes = []

    for recipe in initial_recipes:
        # Nutritionist analyzes recipe
        analysis = nutritionist_analyze_course(recipe, dietary_requirements)

        if analysis["approved"] and not analysis["needs_modification"]:
            # Recipe is good, no changes needed
            logger.info("Recipe approved without changes", extra={
                "workflow": "recipe_refinement",
                "course": recipe["course_type"]
            })
            refined_recipes.append({
                **recipe,
                "nutrition_approved": True,
                "nutrition_analysis": analysis["analysis"]
            })
        else:
            # Recipe needs modification - retry with feedback
            logger.info("Recipe needs refinement", extra={
                "workflow": "recipe_refinement",
                "course": recipe["course_type"],
                "feedback_provided": analysis.get("analysis", "")[:100]
            })

            # Extract modification suggestions from analysis
            feedback = analysis.get("analysis", "Please improve nutritional balance")

            # Chef retries with feedback (max 3 iterations total)
            max_iterations = 3
            current_iteration = recipe.get("iteration", 1)

            if current_iteration < max_iterations:
                # Re-create recipe with feedback
                course_info = {
                    "type": recipe["course_type"],
                    "theme": "refined",
                    "key_ingredients": []
                }

                refined_recipe = chef_create_course_recipe(
                    course_info,
                    dietary_requirements,
                    iteration=current_iteration + 1,
                    feedback=feedback
                )

                # Re-analyze
                second_analysis = nutritionist_analyze_course(refined_recipe, dietary_requirements)

                refined_recipes.append({
                    **refined_recipe,
                    "nutrition_approved": second_analysis["approved"],
                    "nutrition_analysis": second_analysis["analysis"],
                    "refined": True
                })
            else:
                # Max iterations reached, accept as-is
                refined_recipes.append({
                    **recipe,
                    "nutrition_approved": False,
                    "nutrition_analysis": analysis["analysis"],
                    "max_iterations_reached": True
                })

    logger.info("Recipe refinement completed", extra={
        "workflow": "recipe_refinement",
        "total_recipes": len(refined_recipes),
        "approved_count": sum(1 for r in refined_recipes if r.get("nutrition_approved", False))
    })

    return refined_recipes


# Sub-workflow 3: Wine Pairing for All Courses
@workflow(name="wine_pairing_workflow")
def pair_wines_with_courses(refined_recipes, budget):
    """
    Nested Workflow + Retry: Sommelier pairs wines with each course
    Demonstrates error handling with automatic retries
    """
    logger.info("Starting wine pairing", extra={
        "workflow": "wine_pairing",
        "num_courses": len(refined_recipes),
        "budget": budget
    })

    wine_pairings = []

    for recipe in refined_recipes:
        # Sommelier pairs wine (includes built-in retry logic)
        pairing = sommelier_pair_wine_with_course(recipe, budget, attempt=1)
        wine_pairings.append(pairing)

    logger.info("Wine pairing completed", extra={
        "workflow": "wine_pairing",
        "pairings_created": len(wine_pairings)
    })

    return wine_pairings


# Main Workflow: Restaurant Menu Design
@app.route("/menu/design", methods=["POST"])
@workflow(name="restaurant_menu_design_workflow")
def design_restaurant_menu():
    """
    MAIN MULTI-AGENTIC WORKFLOW
    Orchestrates 4 AI agents to design a complete restaurant menu

    Demonstrates:
    - Parallel Execution (agents work simultaneously)
    - Nested Workflows (sub-workflows within main workflow)
    - Decision Trees (coordinator makes strategic choices)
    - Error Handling & Retries (agents retry on failures)
    - Cross-Model Traces (GPT-4 and Claude mixed)

    Request format:
    {
        "cuisine": "Italian",
        "menu_type": "fine_dining",
        "courses": 5,
        "dietary_requirements": ["vegetarian_option", "gluten_free"],
        "budget": "premium",
        "season": "spring",
        "occasion": "romantic_dinner"
    }
    """
    start_time = time.time()

    logger.info("Starting restaurant menu design", extra={
        "workflow": "restaurant_menu_design",
        "endpoint": "/menu/design",
        "method": "POST",
        "remote_addr": request.remote_addr
    })

    try:
        data = request.get_json()

        if not data:
            return jsonify({"error": "Request body must be JSON"}), 400

        # Extract parameters with defaults
        cuisine = data.get("cuisine", "Contemporary")
        menu_type = data.get("menu_type", "fine_dining")
        courses = data.get("courses", 5)
        dietary_requirements = data.get("dietary_requirements", [])
        budget = data.get("budget", "premium")
        season = data.get("season", "current")
        occasion = data.get("occasion", "special_occasion")

        logger.info("Menu design parameters", extra={
            "workflow": "restaurant_menu_design",
            "cuisine": cuisine,
            "courses": courses,
            "menu_type": menu_type,
            "dietary_count": len(dietary_requirements)
        })

        # PHASE 1: Coordinator Plans Menu Structure (Decision Tree)
        logger.info("Phase 1: Coordinator planning", extra={"workflow": "restaurant_menu_design", "phase": 1})
        menu_plan = coordinator_plan_menu_structure(
            cuisine, menu_type, courses, dietary_requirements, budget, season, occasion
        )

        # PHASE 2: Parallel Agent Research (Parallel Execution)
        logger.info("Phase 2: Parallel research", extra={"workflow": "restaurant_menu_design", "phase": 2})
        research_results = parallel_agent_research(menu_plan, dietary_requirements, budget)

        # PHASE 3: Recipe Refinement (Nested Workflow + Error Handling)
        logger.info("Phase 3: Recipe refinement", extra={"workflow": "restaurant_menu_design", "phase": 3})
        refined_recipes = refine_recipes_with_feedback(
            research_results["recipes"],
            dietary_requirements
        )

        # PHASE 4: Wine Pairing (Nested Workflow + Retry Logic)
        logger.info("Phase 4: Wine pairing", extra={"workflow": "restaurant_menu_design", "phase": 4})
        wine_pairings = pair_wines_with_courses(refined_recipes, budget)

        # PHASE 5: Final Assembly
        logger.info("Phase 5: Final assembly", extra={"workflow": "restaurant_menu_design", "phase": 5})

        # Combine recipes with wine pairings
        final_menu = []
        for i, recipe in enumerate(refined_recipes):
            course_data = {
                "course_number": i + 1,
                "course_type": recipe["course_type"],
                "recipe": recipe["recipe"],
                "wine_pairing": wine_pairings[i]["wine_pairing"] if i < len(wine_pairings) else "House selection",
                "nutrition_approved": recipe.get("nutrition_approved", False),
                "nutrition_notes": recipe.get("nutrition_analysis", "")[:200],
                "iterations": recipe.get("iteration", 1),
                "wine_attempts": wine_pairings[i].get("attempt", 1) if i < len(wine_pairings) else 1
            }
            final_menu.append(course_data)

        duration = time.time() - start_time

        logger.info("Restaurant menu design completed", extra={
            "workflow": "restaurant_menu_design",
            "courses_created": len(final_menu),
            "duration_seconds": round(duration, 2),
            "cuisine": cuisine,
            "remote_addr": request.remote_addr
        })

        return jsonify({
            "success": True,
            "menu": {
                "concept": menu_plan.get("menu_concept", ""),
                "cuisine": cuisine,
                "type": menu_type,
                "courses": final_menu,
                "dietary_compliance": dietary_requirements,
                "budget": budget,
                "season": season
            },
            "metadata": {
                "total_courses": len(final_menu),
                "nutrition_approved_count": sum(1 for c in final_menu if c.get("nutrition_approved", False)),
                "total_iterations": sum(c.get("iterations", 1) for c in final_menu),
                "total_wine_attempts": sum(c.get("wine_attempts", 1) for c in final_menu),
                "design_time_seconds": round(duration, 2),
                "agents_involved": ["Menu Coordinator (GPT-5 Mini)", "Executive Chef (Claude)", "Sommelier (GPT-5 Nano)", "Nutritionist (Claude)"]
            }
        }), 200

    except Exception as e:
        duration = time.time() - start_time
        logger.error("Restaurant menu design failed", extra={
            "workflow": "restaurant_menu_design",
            "error": str(e),
            "error_type": type(e).__name__,
            "duration_seconds": round(duration, 2),
            "remote_addr": request.remote_addr
        })
        return jsonify({"error": "Menu design failed", "details": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
